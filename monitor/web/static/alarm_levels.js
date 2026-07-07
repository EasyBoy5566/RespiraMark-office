/* 警報分級對照表 — 共用模組（分工規範見 CLAUDE.md §5；資料來源見 PROTOCOL.md「警報分級」）
 * MEDIBUS 警報碼本身沒有臨床嚴重度分級，且 codepage 1（27H）與 codepage 2（2EH）的
 * code 會重複但意義不同，所以用 "cp:code" 當對照表 key。
 *
 * ★ 這份表由使用者維護（不在 Pi 端）：好處是改一次、所有連線中的 Pi 立刻套用，
 *   不需要改 Pi 端程式碼或重啟呼吸器監測程式。
 *
 * level： 1 = 危及生命／紅（IFU: High, "!!!"）　2 = 可能危及生命／黃（Medium, "!!"）
 *         3 = 不影響生命／淡藍（Low, "!"，IFU 原文為 Turquoise）
 * 查無對照的警報碼 → 用 DEFAULT_LEVEL（保守預設，不會被誤判成不重要）+ 裝置原始縮寫文字。
 *
 * short/full 皆為 MEDIBUS.X Profile Definition（edition 22）原文英文，未翻譯、未改寫：
 *   short = Alarm phrase（裝置實際送出的縮寫，跟 wire 上的 text 對照用）
 *   full  = Alarm description（完整名稱，跟呼吸器螢幕上顯示的較接近，UI 顯示用這個）
 * 分級來源：Evita V300 / Evita Infinity V500 IFU 的「Alarm – Cause – Remedy」章節
 * （兩型號在此章節的分級完全一致，135 筆共同警報 0 衝突）。以下分四段：
 *   1) 已確認：分級 + 名稱皆交叉比對確認，信心度高
 *   2) IFU 對同一警報名稱給了兩種分級：已先取一個，行末註解列出兩個候選
 *   3) 中信心度猜測：字面相似但非精確比對，行末註解列出猜測依據
 *   4) 查無對照：IFU 章節找不到對應（含疑似 SmartCare 等非本章節訊息），level 為預設值
 * 2~4 段皆待使用者依實機或官方文件核對後調整。
 *
 * 全域命名空間：RMAlarm（原生 JS，無模組系統）
 */
"use strict";

const RMAlarm = (() => {
  const DEFAULT_LEVEL = 2;

  // key 格式 "cp:code"（cp 與 code 皆為字串，需與 PROTOCOL.md 的 alarm.alarms[].cp/code 一致）。
  const TABLE = {
    // ===== 已確認（IFU 分級 + MEDIBUS.X 交叉比對，信心度高）=====
    "1:10": { level: 1, short: "PAW HIGH", full: "Airway pressure high" }, // Airway pressure high [!!!/205]
    "1:3E": { level: 1, short: "CO2 ZERO CAL", full: "CO2 zero-calibration requested" }, // CO2 zero calibration? [!!!/142]
    "1:90": { level: 1, short: "RESP RATE HI", full: "Respiratory rate high" }, // Respiratory rate high [!!!/150]
    "1:9A": { level: 1, short: "PAW LOW", full: "Airway pressure low" }, // Airway pressure low [!!!/200]
    "1:A3": { level: 1, short: "PAW NEGATIVE", full: "Airway pressure negative" }, // Airway pressure negative [!!!/140]
    "1:AE": { level: 3, short: "CONT. NEBUL.", full: "Continuous nebulization active" }, // Continuous nebulization activated [!/100]
    "1:C9": { level: 1, short: "INT.TMP.HIGH", full: "Internal device temperature too high" }, // Device temperature high [!!!/200]
    "1:D9": { level: 1, short: "CO2 SENSOR ?", full: "CO2 sensor disconnected or fault" }, // CO2 sensor? [!!!/146]
    "2:93": { level: 2, short: "APNEA VENT", full: "Apnea ventilation" }, // Apnea Ventilation [!!/230]
    "2:99": { level: 3, short: "INSPHOLD END", full: "Inspiration hold aborted" }, // Inspiratory hold interrupted [!/150]
    "2:9C": { level: 3, short: "LEAKAGE", full: "Leakage" }, // Leakage [!/140]
    "2:9E": { level: 3, short: "EXPHOLD END", full: "Expiration hold aborted" }, // Expiratory hold interrupted [!/150]
    "2:CC": { level: 1, short: "PEEP LOW", full: "PEEP low" }, // PEEP low [!!!/140]
    "2:CE": { level: 2, short: "HOSE KINKED", full: "Hose kinked" }, // Breathing hose kinked [!!/205]
    "2:D6": { level: 2, short: "HOSES MIXED?", full: "Hoses interchanged?" }, // Breathing hoses interchanged [!!/105]
    "2:F9": { level: 1, short: "PLOW HIGH", full: "Plow high" }, // Plow high [!!!/140]
    "2:FA": { level: 1, short: "PLOW LOW", full: "Plow low" }, // Plow low [!!!/140]

    // ===== IFU 對同一警報名稱給了兩種分級，先取一個，請自行核對後調整 =====
    "1:4B": { level: 2, short: "BATTERY LOW", full: "Battery capacity low" }, // Battery low listed twice in IFU: [(2, '251'), (3, '250')] -- pick one
    "1:DA": { level: 2, short: "PEEP HIGH", full: "PEEP HIGH" }, // PEEP high listed twice in IFU: [(2, '140'), (3, '140')] -- pick one
    "2:91": { level: 2, short: "LOSS OF DATA", full: "Loss of data" }, // Data loss listed twice in IFU: [(2, '252'), (3, '252')] -- pick one

    // ===== 中信心度猜測（字面相似但非精確比對），請自行核對 =====
    "1:64": { level: 1, short: "CLEAN CO2", full: "Clean CO2 sensor (window occluded)" }, // guess: Clean CO2 cuvette [!!!/144]
    "1:6A": { level: 1, short: "CO2 ERR", full: "CO2 device failure" }, // guess: CO2 measurement failed [!!!/145]
    "1:98": { level: 1, short: "APNEA RESP", full: "Apnea detected by respiratory monitoring" }, // guess: Apnea [!!!/181]
    "1:C4": { level: 3, short: "PRESSURE LIM", full: "Pressure limited respiratory volume" }, // guess: Pressure limited [!/140]
    "1:FF": { level: 1, short: "DISCONNECT", full: "Disconnection ventilator" }, // guess: Disconnection? [!!!/200]
    "2:D8": { level: 3, short: "ID-FUNC-INOP", full: "Accessory ID detection functions inoperable" }, // guess: Accessory ID detection failed [!/060]

    // ===== 查無對照（IFU 章節找不到，或疑似 SmartCare 等非警報分級訊息類）=====
    // level 一律先填 DEFAULT_LEVEL(2)，請自行依實機或文件確認
    "1:08": { level: 2, short: "% O2 LOW", full: "Inspiratory oxygen concentration < low limit" },
    "1:12": { level: 2, short: "AIR SUPPLY ?", full: "Check air supply" },
    "1:13": { level: 2, short: "O2 SUPPLY ?", full: "Check O2 supply" },
    "1:19": { level: 2, short: "MIN VOL LOW", full: "Minute volume < low limit" },
    "1:27": { level: 2, short: "ET CO2 LOW", full: "End-tidal CO2 < low limit" },
    "1:28": { level: 2, short: "ET CO2 HIGH", full: "End-tidal CO2 > high limit" },
    "1:33": { level: 2, short: "VOL INCONST", full: "Volume not constant" },
    "1:37": { level: 2, short: "% O2 HIGH", full: "Inspiratory oxygen concentration > high limit" },
    "1:42": { level: 2, short: "FLOW SENSOR?", full: "Check flow sensor" },
    "1:65": { level: 2, short: "SPEAKER FAIL", full: "Speaker failure" },
    "1:78": { level: 2, short: "RS232COM ERR", full: "Communication error RS232 port" },
    "1:9B": { level: 2, short: "MIN VOL HIGH", full: "Minute volume > high limit" },
    "1:9F": { level: 2, short: "VENT ERR", full: "Problems with respirator" },
    "1:A1": { level: 2, short: "% O2 ERR", full: "Inspiratory O2 measurement inoperable" },
    "1:A2": { level: 2, short: "VOL CAL ?", full: "Flow calibration necessary" },
    "1:A4": { level: 2, short: "VOL ERR", full: "Volume measurement inoperable" },
    "1:AD": { level: 2, short: "PRESS ERR", full: "Pressure measurement inoperable" },
    "1:B0": { level: 2, short: "EXP-VALVE ?", full: "Check expiratory valve" },
    "1:B8": { level: 2, short: "AW-TEMP INOP", full: "Airway temperature measurement inop" },
    "1:B9": { level: 2, short: "AW-TEMP SENS", full: "Check airway temperature sensor" },
    "1:E6": { level: 2, short: "AIR PRESS HI", full: "Air supply pressure > high limit" },
    "1:E7": { level: 2, short: "HI O2 SUPPLY", full: "High O2 supply pressure" },
    "1:E8": { level: 2, short: "TIDAL VOL HI", full: "Tidal volume > high limit" },
    "1:EC": { level: 2, short: "GAS FAILURE", full: "Gas supply failure" },
    "1:F2": { level: 2, short: "SYSTEM FAULT", full: "Internal system fault" },
    "1:FC": { level: 2, short: "CO2 NOT CAL", full: "CO2 not calibrated" },
    "1:FD": { level: 2, short: "BATTERY ERR", full: "Battery inoperable" },
    "1:FE": { level: 2, short: "COOLING ?", full: "Check cooling" },
    "2:36": { level: 2, short: "NO OXYGEN", full: "O2 delivery failure" },
    "2:37": { level: 2, short: "NO AIR", full: "AIR delivery failure" },
    "2:3B": { level: 2, short: "AMB PRESS ?", full: "Ambient pressure measurement disturbed or inoperable" },
    "2:5A": { level: 2, short: "BATTERY ON", full: "Power supply by battery" },
    "2:5C": { level: 2, short: "BATT. < 2MIN", full: "Battery less than 2 min left" },
    "2:5D": { level: 2, short: "BATT. < 5MIN", full: "Battery less than 5 min left" },
    "2:6A": { level: 2, short: "TUBE OBSTRUC", full: "Tube obstructed" },
    "2:90": { level: 2, short: "NEO FLOW?", full: "Check neonatal flow sensor" },
    "2:94": { level: 2, short: "CHECK VENT", full: "Check ventilator" },
    "2:9F": { level: 2, short: "NEBULIZ. OFF", full: "Nebulization terminated" },
    "2:A1": { level: 2, short: "PWR SPLY ERR", full: "Problems with power supply" },
    "2:A9": { level: 2, short: "SET.CANCELED", full: "Setting could not be performed" },
    "2:B8": { level: 2, short: "NO CONFIRM.", full: "A setting, alarm limit or ventilation mode was changed, but the change was not confirmed. If necessary, the user can adjust and confirm the setting again with the rotary knob." },
    "2:BA": { level: 2, short: "PMIN REACHED", full: "Delivered volume greater set tidal volume due to minimum required PIP" },
    "2:D1": { level: 2, short: "HOSE ERROR", full: "Hose system defect" },
    "2:D7": { level: 2, short: "WRONG HOSES?", full: "Hoses incompatible?" },
    "2:DA": { level: 2, short: "EXP TIME ERR", full: "Set expiration time not attainable" },
    "2:E5": { level: 2, short: "SC ABORTED", full: "SmartCare: patient session aborted" },
    "2:E6": { level: 2, short: "SC INOP", full: "SmartCare: inoperable / patient session terminated" },
    "2:E7": { level: 2, short: "CENTRAL HYPO", full: "SmartCare: Central Hypoventilation" },
    "2:E8": { level: 2, short: "PERS TACHYP", full: "SmartCare: Persistent Tachypnea" },
    "2:E9": { level: 2, short: "UNEXPL HYPER", full: "SmartCare: Unexplained Hyperventilation" },
    "2:EA": { level: 2, short: "PEEP REDUCIB", full: "SmartCare: Reduce PEEP if possible" },
    "2:EB": { level: 2, short: "CONS SEPARAT", full: "SmartCare: SBT successful" },
    "2:F3": { level: 2, short: "FIO2 REDUCIB", full: "SmartCare: Reduce FiO2 if possible" },
    "2:F8": { level: 2, short: "TIDAL VOL LO", full: "Tidal volume < low Limit" },
  };

  /** alarm: {cp, code, prio, text}（見 PROTOCOL.md）→ {level, name}；name 用 full（跟呼吸器
   * 螢幕上顯示的名稱較接近），查無對照時退回裝置原始縮寫文字 */
  function classify(alarm) {
    const hit = TABLE[`${alarm.cp}:${alarm.code}`];
    return hit
      ? { level: hit.level, name: hit.full }
      : { level: DEFAULT_LEVEL, name: alarm.text || alarm.code || "ALARM" };
  }

  return { classify, DEFAULT_LEVEL, TABLE };
})();
