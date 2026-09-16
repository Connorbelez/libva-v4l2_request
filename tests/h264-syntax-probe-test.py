#!/usr/bin/env python3
"""Synthetic syntax and picture-identity regressions; no corpus or device."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('probe', Path(__file__).with_name('h264-syntax-probe.py'))
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

def ue(n):
    b = bin(n + 1)[2:]
    return '0' * (len(b) - 1) + b

def packed(bits):
    bits += '1'  # rbsp_stop_one_bit
    bits += '0' * (-len(bits) % 8)
    return int(bits, 2).to_bytes(len(bits) // 8, 'big')

def sps(ident=0, frame_bits_minus4=0):
    return packed('01001101' + '00000000' + '00011110' + ue(ident) +
                  ue(frame_bits_minus4) + ue(0) + ue(0) + ue(1) + '0' +
                  ue(7) + ue(7) + '1' + '1' + '0' + '0')

def pps(ident=0, seq=0, groups_minus1=0, map_type=0):
    b = ue(ident) + ue(seq) + '00' + ue(groups_minus1)
    if groups_minus1:
        b += ue(map_type)
        if map_type == 2:
            b += (ue(0) + ue(3)) * groups_minus1
        elif map_type == 0:
            b += ue(0) * (groups_minus1 + 1)
    b += ue(0) + ue(0) + '000' + ue(0) * 3 + '100'
    return packed(b)

def slice_(mb, poc=0, pps_id=0):
    return packed(ue(mb) + ue(2) + ue(pps_id) + '0000' + f'{poc:04b}')

def nal(header, payload):
    return b'\x00\x00\x01' + bytes([header]) + payload

class Syntax(unittest.TestCase):
    def test_sps_has_no_slice_group_fields(self):
        result = p.parse_sps(sps())
        self.assertTrue(result['parse_ok'])
        self.assertNotIn('num_slice_groups_minus1', result)

    def test_type_two_pps_has_one_rectangle_per_nonbackground_group(self):
        result = p.parse_pps(pps(groups_minus1=1, map_type=2))
        self.assertTrue(result['parse_ok'])
        self.assertEqual(result['num_slice_groups_minus1'], 1)
        self.assertEqual(result['slice_group_map_type'], 2)

    def test_hrd_consumes_both_scales_and_four_delay_lengths(self):
        bits = ue(0) + '00000000' + ue(0) + ue(0) + '0' + '0' * 20
        reader = p.BitReader(packed(bits + '10101'))
        p.skip_hrd_parameters(reader)
        self.assertEqual(reader.u(5), 0b10101)

    def test_slice_uses_referenced_sps_not_last_seen(self):
        seqs = {0: p.parse_sps(sps()), 1: p.parse_sps(sps(1, 4))}
        pictures = {0: p.parse_pps(pps(), seqs)}
        result = p.parse_slice_prefix(slice_(0, 7), seqs, pictures, 1, 3)
        self.assertEqual(result['pic_order_cnt_lsb'], 7)

    def probe(self, slices):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic.h264'
            path.write_bytes(nal(0x67, sps()) + nal(0x68, pps()) + b''.join(nal(0x61, x) for x in slices))
            return p.probe_file(path)

    def test_repeated_frame_num_in_different_pictures_is_not_aso(self):
        result = self.probe([slice_(0, 0), slice_(30, 0), slice_(0, 2), slice_(30, 2)])
        self.assertEqual(result['errors'], [])
        self.assertNotIn('slice_order_not_monotonic', result['features'])

    def test_descending_slice_addresses_in_same_picture_is_aso(self):
        result = self.probe([slice_(30, 0), slice_(0, 0)])
        self.assertEqual(result['features']['slice_order_not_monotonic'], 1)

    def test_unparsed_pps_is_visible_and_not_trusted(self):
        result = p.parse_pps(b'\0')
        self.assertFalse(result['parse_ok'])

if __name__ == '__main__':
    unittest.main()
