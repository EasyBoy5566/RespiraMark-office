# -*- coding: utf-8 -*-
"""
test_ward — 床號 → 單位推導（PROTOCOL.md「單位」）
====================================================
案例取自院方設備系統 `getDeviceList` 的**實際回傳床號**與院內病房編碼規律，
不是編出來的——這條規則唯一的依據就是現場資料長什麼樣。

鎖住最容易在未來改動時弄壞的事：
- 加護單位代碼長度不一（`MI` 兩碼、`CCU`/`RCC` 三碼）都要對
- 一般病房的三種樓層換算（N +20、R 同號、M/MP +80）不能互相搞混
- 樓層碼認不得時要退回「一般病房」，**絕不能變成未指定**——那會讓在病房
  使用的呼吸器因為篩選不到而整台從看板消失
- 認不得的格式一律空字串，絕不猜一個看起來像真的的單位

用法（專案根目錄）：
    python -m unittest tests.test_ward -v
"""
import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from monitor.domain.ward import GENERAL_WARD, from_bed


class IcuWardTest(unittest.TestCase):
    """加護／特殊單位：病房代碼直接寫在床號開頭"""

    def test_icu_codes(self):
        for bed, expected in [("MI01", "MI"), ("MI09", "MI"), ("MI22", "MI"),
                              ("CCU01", "CCU"), ("CCU17", "CCU"),
                              ("SI02", "SI"), ("SI27", "SI"),
                              ("NI01", "NI"), ("NI35", "NI"),
                              ("PI06", "PI"), ("PI19", "PI"),
                              ("RCC03", "RCC"), ("RCC21", "RCC"),
                              ("IR01", "IR"), ("BR21", "BR"),
                              ("BC02", "BC")]:            # 燒傷加護
            self.assertEqual(from_bed(bed), expected, bed)

    def test_hospice_has_extra_bed_suffix(self):
        """安寧（HP）多一個床位序號，不影響單位判定"""
        for bed in ("HP12-1", "HP12-2", "HP13-1", "HP26"):
            self.assertEqual(from_bed(bed), "HP", bed)

    def test_code_missing_from_official_list(self):
        """燒傷加護（BC）是真實單位，卻不在院方 33 個單位的代碼表裡——
        代碼表本身就不完整。寫死清單會讓 BC02 比對不到而整台從看板消失，
        取字母前綴才不會（見 ward.py 檔頭警告）"""
        self.assertEqual(from_bed("BC02"), "BC")


class GeneralWardTest(unittest.TestCase):
    """一般病房：{樓層2碼}{房間2碼}-{床位}，三個系列各有各的樓層換算"""

    def test_n_series_floor_plus_20(self):
        """N 系列：樓層碼 = 病房號 + 20"""
        for bed, expected in [("2812-1", "N08"), ("2813-2", "N08"),
                              ("2912-2", "N09"), ("3011-1", "N10"),
                              ("3112-1", "N11"), ("3211-1", "N12"),
                              ("3311-1", "N13"), ("3412-1", "N14"),
                              ("3511-1", "N15"), ("3611-1", "N16"),
                              ("3712-1", "N17")]:
            self.assertEqual(from_bed(bed), expected, bed)

    def test_r_series_floor_same_number(self):
        """R 系列：樓層碼直接對應，不加 20（且沒有 R08）"""
        for bed, expected in [("0711-1", "R07"), ("0912-1", "R09"),
                              ("1011-1", "R10"), ("1111-2", "R11"),
                              ("1211-1", "R12"), ("1311-2", "R13"),
                              ("1413-1", "R14")]:
            self.assertEqual(from_bed(bed), expected, bed)

    def test_m_series_floor_plus_80(self):
        """M／MP 系列：樓層碼 = 病房號 + 80；身心科一房一床，沒有床位序號"""
        self.assertEqual(from_bed("8811-1"), "M08")
        self.assertEqual(from_bed("8911-1"), "M09")
        self.assertEqual(from_bed("8611"), "MP06")
        self.assertEqual(from_bed("8711"), "MP07")

    def test_real_device_list_rooms(self):
        """實際設備資料裡的一般病房床號（先前只能顯示「一般病房」）"""
        self.assertEqual(from_bed("3520-1"), "N15")
        self.assertEqual(from_bed("3517-1"), "N15")
        self.assertEqual(from_bed("3236-1"), "N12")

    def test_unknown_floor_falls_back_not_unassigned(self):
        """樓層碼不在表上仍要歸一般病房。退回「未指定」的話，那台機器會在
        看板的單位篩選中整個消失——寧可粗一點也不能讓機器不見"""
        self.assertEqual(from_bed("9911-1"), GENERAL_WARD)
        self.assertEqual(from_bed("0111"), GENERAL_WARD)


class EdgeCaseTest(unittest.TestCase):
    def test_missing_bed(self):
        """沒有床號（devices.json 尚未帶入，目前的常態）"""
        for bed in ("", "   ", None):
            self.assertEqual(from_bed(bed), "")

    def test_unrecognised_never_guesses(self):
        """認不得的格式一律空字串：床號來自院方系統、格式不在我們掌握中，
        寧可顯示未指定，也不要編一個看起來像真的的單位"""
        for bed in ("-", "???", "12", "12345", "35-1"):
            self.assertEqual(from_bed(bed), "", bed)

    def test_case_normalised(self):
        self.assertEqual(from_bed("mi09"), "MI")

    def test_whitespace_trimmed(self):
        self.assertEqual(from_bed("  CCU03  "), "CCU")


if __name__ == "__main__":
    unittest.main(verbosity=2)
