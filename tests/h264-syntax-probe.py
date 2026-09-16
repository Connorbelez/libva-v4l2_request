#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Measure advanced H.264 syntax in conformance vectors without a device.

Why this exists: the usual shortcut for reading H.264 fields is ffmpeg's
``trace_headers`` bitstream filter, but ffmpeg refuses to decode FMO streams
("FMO is not implemented") and then cannot parse the slice headers of exactly the
vectors under study. For a boundary decision about FMO, ASO and data partitions,
the measurement has to come from the bitstream itself.

This probe reads Annex B NAL units, de-emulates the RBSP and decodes the
sequence parameter set, picture parameter set and the slice-header prefix. It
reports only fields that a userspace driver can act on before submission:

  profile/level, chroma format and depth, frame_mbs_only_flag and MBAFF,
  num_slice_groups_minus1 and slice_group_map_type (FMO), the slice types
  present (SP/SI included), redundant_pic_cnt_present_flag, partition NAL
  presence (types 2/3/4), extension NAL presence (20/21), and per-picture
  first_mb_in_slice ordering, which is how arbitrary slice order shows up.

Deliberate limits: the slice-group change cycle value, reference list
modification, prediction weights and later slice-header fields are not decoded;
SPS/PPS fields that only matter after the header prefix are not asserted. A field
this probe cannot reach is reported as unknown rather than guessed.

Usage:
  python3 tests/h264-syntax-probe.py FILE [FILE ...] [--json OUT] [--quiet]
  python3 tests/h264-syntax-probe.py --suite JVT-AVC_V1 --vectors-from docs/r11-pass-sets.json \
      --fluster DIR --cache DIR --json docs/h264-advanced-syntax-matrix.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

HIGH_PROFILES_WITH_CHROMA = {
    100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135,
}
# profile_idc values defined by H.264. A value outside this set means the walk
# drifted and the fields must not be trusted.
KNOWN_PROFILES = {66, 77, 88} | HIGH_PROFILES_WITH_CHROMA
PARTITION_NAL_TYPES = {2: "partition-a", 3: "partition-b", 4: "partition-c"}
SLICE_NAMES = {0: "P", 1: "B", 2: "I", 3: "SP", 4: "SI"}


class SyntaxError_(Exception):
    """The bitstream could not be read far enough to answer the question."""


class BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def bits_left(self) -> int:
        return len(self.data) * 8 - self.pos

    def u(self, count: int) -> int:
        if count < 0 or self.bits_left() < count:
            raise SyntaxError_(f"read of {count} bits at offset {self.pos} exceeds the RBSP")
        value = 0
        for _ in range(count):
            byte = self.data[self.pos >> 3]
            value = (value << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return value

    def bit(self) -> int:
        return self.u(1)

    def ue(self) -> int:
        zeros = 0
        while True:
            if self.bits_left() <= 0:
                raise SyntaxError_("unterminated exp-Golomb code")
            if self.bit():
                break
            zeros += 1
            if zeros > 31:
                raise SyntaxError_("exp-Golomb code longer than 31 leading zeros")
        return (1 << zeros) - 1 + (self.u(zeros) if zeros else 0)

    def se(self) -> int:
        code = self.ue()
        magnitude = (code + 1) // 2
        return magnitude if code % 2 else -magnitude

    def trailing_bits_ok(self) -> bool:
        """True when the remaining bits are exactly rbsp_trailing_bits (one 1, then zeros).

        This is the parser's self-check: if the SPS/PPS walk drifted, the invariant
        fails and the fields read from it must not be trusted.
        """
        if self.bits_left() <= 0:
            return False
        seen_stop = False
        for index in range(self.pos, len(self.data) * 8):
            bit = (self.data[index >> 3] >> (7 - (index & 7))) & 1
            if not seen_stop:
                if bit != 1:
                    return False
                seen_stop = True
            elif bit != 0:
                return False
        return seen_stop

    def more_rbsp_data(self) -> bool:
        """True when bits remain before the rbsp_stop_one_bit and its padding."""
        if self.bits_left() <= 0:
            return False
        last_one = None
        for index in range(len(self.data) * 8 - 1, self.pos - 1, -1):
            if (self.data[index >> 3] >> (7 - (index & 7))) & 1:
                last_one = index
                break
        return last_one is not None and self.pos < last_one

    def skip(self, count: int) -> None:
        if count:
            self.u(count)


def split_annex_b(data: bytes) -> list[tuple[int, bytes]]:
    """Return (nal_unit_type, rbsp) for every NAL unit, de-emulating the RBSP."""
    starts: list[int] = []
    index = 0
    while True:
        found = data.find(b"\x00\x00\x01", index)
        if found < 0:
            break
        starts.append(found + 3)
        index = found + 3
    units = []
    for position, start in enumerate(starts):
        end = starts[position + 1] - 3 if position + 1 < len(starts) else len(data)
        # A four-byte start code leaves a trailing zero of the previous unit.
        while end > start and data[end - 1] == 0:
            end -= 1
        payload = data[start:end]
        if not payload:
            continue
        # The first byte is the NAL header; the RBSP of the payload follows it.
        body = payload[1:]
        rbsp = bytearray()
        zeros = 0
        for byte in body:
            if zeros >= 2 and byte == 3:
                zeros = 0
                continue
            zeros = zeros + 1 if byte == 0 else 0
            rbsp.append(byte)
        units.append((payload[0] & 0x1F, bytes(rbsp)))
    return units


def skip_scaling_list(reader: BitReader, size: int) -> None:
    last_scale = 8
    next_scale = 8
    for _ in range(size):
        if next_scale != 0:
            delta = reader.se()
            next_scale = (last_scale + delta + 256) % 256
        last_scale = next_scale if next_scale != 0 else last_scale


def skip_hrd_parameters(reader: BitReader) -> None:
    cpb_cnt = reader.ue() + 1
    reader.skip(4)  # bit_rate_scale, cpb_size_scale
    for _ in range(cpb_cnt):
        reader.ue()  # bit_rate_value_minus1
        reader.ue()  # cpb_size_value_minus1
        reader.bit()  # cbr_flag
    reader.skip(5 + 5 + 5 + 5 + 5)  # initial_cpb_removal_delay_length_minus1 .. time_offset_length


def skip_vui(reader: BitReader) -> None:
    if reader.bit():  # aspect_ratio_info_present_flag
        aspect = reader.u(8)
        if aspect == 255:  # Extended_SAR
            reader.skip(16 + 16)
    if reader.bit():  # overscan_info_present_flag
        reader.bit()
    if reader.bit():  # video_signal_type_present_flag
        reader.skip(3 + 1)  # video_format, video_full_range_flag
        if reader.bit():  # colour_description_present_flag
            reader.skip(8 + 8 + 8)
    if reader.bit():  # chroma_loc_info_present_flag
        reader.ue()
        reader.ue()
    if reader.bit():  # timing_info_present_flag
        reader.skip(32 + 32)
        reader.bit()  # fixed_frame_rate_flag
    nal_hrd = reader.bit()
    if nal_hrd:
        skip_hrd_parameters(reader)
    vcl_hrd = reader.bit()
    if vcl_hrd:
        skip_hrd_parameters(reader)
    if nal_hrd or vcl_hrd:
        reader.bit()  # low_delay_hrd_flag
    reader.bit()  # pic_struct_present_flag
    if reader.bit():  # bitstream_restriction_flag
        reader.bit()  # motion_vectors_over_pic_boundaries_flag
        for _ in range(2):
            reader.ue()  # max_bytes_per_pic_denom, max_bits_per_mb_denom
        for _ in range(4):
            reader.ue()  # log2_max_mv_length_horizontal/vertical, max_num_reorder_frames, max_dec_frame_buffering


def parse_sps(rbsp: bytes) -> dict:
    reader = BitReader(rbsp)
    fields: dict = {}
    fields["profile_idc"] = reader.u(8)
    reader.skip(8)  # constraint flags and reserved bits
    fields["level_idc"] = reader.u(8)
    fields["seq_parameter_set_id"] = reader.ue()
    fields["chroma_format_idc"] = 1
    fields["bit_depth_luma_minus8"] = 0
    if fields["profile_idc"] in HIGH_PROFILES_WITH_CHROMA:
        fields["chroma_format_idc"] = reader.ue()
        if fields["chroma_format_idc"] == 3:
            fields["separate_colour_plane_flag"] = reader.bit()
        fields["bit_depth_luma_minus8"] = reader.ue()
        fields["bit_depth_chroma_minus8"] = reader.ue()
        reader.bit()  # qpprime_y_zero_transform_bypass_flag
        if reader.bit():  # seq_scaling_matrix_present_flag
            count = 8 if fields["chroma_format_idc"] != 3 else 12
            for index in range(count):
                if reader.bit():  # seq_scaling_list_present_flag
                    skip_scaling_list(reader, 16 if index < 6 else 64)
    fields["log2_max_frame_num_minus4"] = reader.ue()
    fields["pic_order_cnt_type"] = reader.ue()
    if fields["pic_order_cnt_type"] == 0:
        fields["log2_max_pic_order_cnt_lsb_minus4"] = reader.ue()
    elif fields["pic_order_cnt_type"] == 1:
        reader.bit()  # delta_pic_order_always_zero_flag
        reader.se()
        reader.se()
        for _ in range(reader.ue()):  # num_ref_frames_in_pic_order_cnt_cycle
            reader.se()
    fields["max_num_ref_frames"] = reader.ue()
    reader.bit()  # gaps_in_frame_num_value_allowed_flag
    fields["pic_width_in_mbs_minus1"] = reader.ue()
    fields["pic_height_in_map_units_minus1"] = reader.ue()
    fields["frame_mbs_only_flag"] = reader.bit()
    fields["mb_adaptive_frame_field_flag"] = 0
    if not fields["frame_mbs_only_flag"]:
        fields["mb_adaptive_frame_field_flag"] = reader.bit()
    reader.bit()  # direct_8x8_inference_flag
    if reader.bit():  # frame_cropping_flag
        for _ in range(4):
            reader.ue()
    if reader.bit():  # vui_parameters_present_flag
        skip_vui(reader)
    fields["num_slice_groups_minus1"] = 0
    fields["slice_group_map_type"] = None
    if reader.more_rbsp_data():
        fields["num_slice_groups_minus1"] = reader.ue()
        if fields["num_slice_groups_minus1"] > 0:
            fields["slice_group_map_type"] = reader.ue()
    fields["parse_ok"] = (
        reader.trailing_bits_ok() and fields["profile_idc"] in KNOWN_PROFILES
    )
    return fields


def parse_pps(rbsp: bytes) -> dict:
    reader = BitReader(rbsp)
    fields: dict = {}
    try:
        return _parse_pps_body(reader, fields)
    except SyntaxError_ as error:
        # A PPS that cannot be walked to its end still tells us whether it
        # declares slice groups, which is the signal that matters here.
        fields["parse_ok"] = False
        fields["parse_error"] = str(error)
        return fields


def _parse_pps_body(reader: BitReader, fields: dict) -> dict:
    fields["pic_parameter_set_id"] = reader.ue()
    fields["seq_parameter_set_id"] = reader.ue()
    reader.bit()  # entropy_coding_mode_flag
    fields["bottom_field_pic_order_in_frame_present_flag"] = reader.bit()
    fields["num_slice_groups_minus1"] = reader.ue()
    # The picture parameter set repeats the slice-group map, so it has to be
    # consumed here or every later field in this PPS is misread on FMO streams.
    if fields["num_slice_groups_minus1"] > 0:
        map_type = reader.ue()
        fields["slice_group_map_type"] = map_type
        groups = fields["num_slice_groups_minus1"] + 1
        if map_type == 0:
            for _ in range(groups):
                reader.ue()  # run_length_minus1
        elif map_type == 2:
            for _ in range(groups):
                reader.ue()  # top_left
                reader.ue()  # bottom_right
        elif map_type in (3, 4, 5):
            reader.bit()  # slice_group_change_direction_flag
            reader.ue()  # slice_group_change_rate_minus1
        elif map_type == 6:
            pic_size = reader.ue()  # pic_size_in_map_units_minus1
            width = max(1, (groups - 1).bit_length())
            for _ in range(pic_size + 1):
                reader.u(width)  # slice_group_id
    reader.ue()  # num_ref_idx_l0_default_active_minus1
    reader.ue()  # num_ref_idx_l1_default_active_minus1
    reader.bit()  # weighted_pred_flag
    reader.skip(2)  # weighted_bipred_idc
    reader.se()  # pic_init_qp_minus26
    reader.se()  # pic_init_qs_minus26
    reader.se()  # chroma_qp_index_offset
    reader.bit()  # deblocking_filter_control_present_flag
    reader.bit()  # constrained_intra_pred_flag
    fields["redundant_pic_cnt_present_flag"] = reader.bit()
    fields["parse_ok"] = reader.trailing_bits_ok()
    return fields


def parse_slice_prefix(rbsp: bytes, sps: dict) -> dict:
    """Decode the slice-header fields needed to detect ordering and slice types."""
    reader = BitReader(rbsp)
    fields: dict = {}
    fields["first_mb_in_slice"] = reader.ue()
    raw_type = reader.ue()
    fields["slice_type_raw"] = raw_type
    fields["slice_type"] = raw_type % 5
    fields["slice_type_name"] = SLICE_NAMES.get(fields["slice_type"], "?")
    fields["pic_parameter_set_id"] = reader.ue()
    fields["frame_num"] = reader.u(sps["log2_max_frame_num_minus4"] + 4)
    if not sps["frame_mbs_only_flag"]:
        fields["field_pic_flag"] = reader.bit()
        if fields["field_pic_flag"]:
            fields["bottom_field_flag"] = reader.bit()
    return fields


def probe_file(path: Path) -> dict:
    data = path.read_bytes()
    units = split_annex_b(data)
    nal_counts = Counter(nal_type for nal_type, _ in units)
    sps_list: list[dict] = []
    pps_list: list[dict] = []
    slices: list[dict] = []
    errors: list[str] = []
    for nal_type, rbsp in units:
        try:
            if nal_type == 7:
                sps_list.append(parse_sps(rbsp))
            elif nal_type == 8:
                pps_list.append(parse_pps(rbsp))
            elif nal_type in (1, 5) and sps_list:
                slices.append(parse_slice_prefix(rbsp, sps_list[-1]))
        except SyntaxError_ as error:
            # One unreadable unit must not hide the features that were measured.
            if len(errors) < 5:
                errors.append(f"nal {nal_type}: {error}")
    features: dict = {}
    # Only fields from a parameter set whose walk ended exactly on
    # rbsp_trailing_bits are trusted; a drifted parse is reported as unknown.
    usable_sps = [s for s in sps_list if s.get("parse_ok")]
    if sps_list and not usable_sps:
        features["sps_parse_failed"] = True
    # The slice-group map lives in both parameter sets: the SPS carries the map
    # itself and the PPS repeats num_slice_groups_minus1 and the map type. Real
    # vectors (FM1_BT_B) declare zero slice groups in the SPS and every map type
    # across eight PPS NALs, so a detector that only reads the SPS misses them.
    group_declarers = [d for d in (sps_list + pps_list) if d.get("num_slice_groups_minus1")]
    if group_declarers:
        features["fmo"] = True
        features["slice_group_counts"] = sorted({d["num_slice_groups_minus1"] + 1 for d in group_declarers})
        features["slice_group_map_types"] = sorted(
            {d.get("slice_group_map_type") for d in group_declarers if d.get("slice_group_map_type") is not None}
        )
        features["fmo_sources"] = sorted(
            {"sps" if d in sps_list else "pps" for d in group_declarers}
        )
        # FMO declared only in a PPS while the active SPS declares none.
        features["fmo_in_pps_only"] = not any(s.get("num_slice_groups_minus1") for s in usable_sps)
        plain_sps = [s for s in usable_sps if not s.get("num_slice_groups_minus1")]
        features["fmo_after_plain_sps"] = bool(plain_sps)
    unparsed_pps = [p for p in pps_list if p.get("num_slice_groups_minus1") is None]
    if unparsed_pps:
        features["pps_unparsed_map_type"] = len(unparsed_pps)
    partitions = {PARTITION_NAL_TYPES[t]: nal_counts[t] for t in PARTITION_NAL_TYPES if nal_counts.get(t)}
    if partitions:
        features["data_partitions"] = partitions
    if usable_sps and not usable_sps[0].get("frame_mbs_only_flag", 1):
        features["mbaff" if usable_sps[0].get("mb_adaptive_frame_field_flag") else "field_coding"] = True
    slice_types = sorted({s["slice_type_name"] for s in slices})
    if slice_types:
        features["slice_types"] = slice_types
    if any(s["slice_type"] in (3, 4) for s in slices):
        features["sp_or_si"] = True
    if any(p.get("parse_ok") and p.get("redundant_pic_cnt_present_flag") for p in pps_list):
        features["redundant_slices"] = True
    if pps_list and not any(p.get("parse_ok") for p in pps_list):
        features["pps_parse_failed"] = True
    # Arbitrary slice order: first_mb_in_slice decreasing inside one coded picture.
    per_picture: dict[tuple, list[int]] = defaultdict(list)
    for s in slices:
        per_picture[(s["frame_num"], s.get("field_pic_flag"), s.get("bottom_field_flag"))].append(
            s["first_mb_in_slice"]
        )
    out_of_order = [key for key, values in per_picture.items() if any(b < a for a, b in zip(values, values[1:]))]
    if out_of_order:
        features["slice_order_not_monotonic"] = len(out_of_order)
    extension = sorted({f"nal-{t}" for t in nal_counts if t in (20, 21)})
    if extension:
        features["extension_nals"] = extension
    return {
        "file": path.name,
        "bytes": len(data),
        "nal_counts": {str(k): v for k, v in sorted(nal_counts.items())},
        "sps_count": len(sps_list),
        "pps_count": len(pps_list),
        "slice_count": len(slices),
        "features": features,
        "errors": errors,
        "sps": sps_list[:4],
        "pps": pps_list[:4],
    }


def suite_vectors(fluster: Path, suite_file: str, names: list[str]) -> dict[str, dict]:
    suite = json.loads((fluster / suite_file).read_text())
    return {v["name"]: v for v in suite["test_vectors"] if v["name"] in set(names)}


def collect(args) -> list[dict]:
    if args.suite:
        names = [n.strip() for n in args.vectors.split(",") if n.strip()] if args.vectors else []
        if args.vectors_from:
            names = sorted(set(json.loads(Path(args.vectors_from).read_text())["suites"]["avc"]["failing_vectors"]))
        suite_file = args.suite_file or "test_suites/h.264/JVT-AVC_V1.json"
        # Cache layout is <cache>/<suite_name>/<vector_name>/<input_file>.
        cache_root = Path(args.cache) / args.suite
        vectors = suite_vectors(Path(args.fluster), suite_file, names)
        missing = sorted(set(names) - set(vectors))
        if missing:
            print(f"warning: {len(missing)} vector(s) not in the pinned suite: {missing[:5]}", file=sys.stderr)
        results = []
        for name in sorted(vectors):
            definition = vectors[name]
            path = cache_root / name / definition["input_file"]
            if not path.is_file():
                results.append({"vector": name, "error": f"input file missing: {path}", "features": {}})
                continue
            record = probe_file(path)
            record["vector"] = name
            record["profile"] = definition.get("profile")
            results.append(record)
        return results
    return [probe_file(Path(p)) for p in args.files]


def summarize(results: list[dict]) -> dict:
    labels = Counter()
    vectors_with: dict[str, list[str]] = defaultdict(list)
    for record in results:
        for label in record.get("features", {}):
            labels[label] += 1
            vectors_with[label].append(record.get("vector") or record.get("file"))
    multi = [r.get("vector") or r.get("file") for r in results if len(r.get("features", {})) > 1]
    return {
        "vectors": len(results),
        "feature_counts": dict(sorted(labels.items())),
        "vectors_per_feature": {k: sorted(v) for k, v in sorted(vectors_with.items())},
        "multi_label_vectors": sorted(multi),
        "errors": [r for r in results if r.get("error")],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--suite", help="Fluster suite name to read from a pinned cache")
    parser.add_argument("--suite-file", help="suite JSON path inside the Fluster checkout")
    parser.add_argument("--fluster", help="pinned Fluster checkout")
    parser.add_argument("--cache", help="Fluster resources cache")
    parser.add_argument("--vectors", help="comma-separated vector names")
    parser.add_argument("--vectors-from", help="JSON file with suites.<key>.failing_vectors")
    parser.add_argument("--json", type=Path, help="write the full matrix here")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    if not args.files and not args.suite:
        parser.error("give files, or --suite with --fluster and --cache")
    results = collect(args)
    summary = summarize(results)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"summary": summary, "vectors": results}, indent=2) + "\n")
    if not args.quiet:
        print(json.dumps(summary["feature_counts"], indent=2))
        print(f"\nvectors probed: {summary['vectors']}")
        print(f"multi-label vectors: {len(summary['multi_label_vectors'])}")
        for label, names in summary["vectors_per_feature"].items():
            print(f"  {label}: {len(names)}")
        if summary["errors"]:
            print(f"errors: {len(summary['errors'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
