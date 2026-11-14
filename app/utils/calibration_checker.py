#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
记录正文对 Sr/Ca 参考材料和数值校正的说明。
"""

import logging
import re
from typing import Dict, Any

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def check_sr_ca_calibration(pdf_text: str) -> Dict[str, Any]:
    """区分 Sr/Ca 参考材料报告和实际数值校正，保留支持语句。"""
    body = re.split(r"(?im)^\s*(?:#+\s*)?(?:references|bibliography|参考文献)\s*$", pdf_text, 1)[0]
    text = " ".join(body.split())
    indicator = r"Sr\s*/\s*Ca|strontium\s*/\s*calcium|锶钙比"
    standard = r"\bJC[pt]\s*[-–]?\s*1\b"
    relevant = bool(re.search(indicator, text, re.I))
    result = {
        "is_calibrated": False,
        "reference_reported": False,
        "calibration_standard": None,
        "related_text": "",
        "reference_unconfirmed": relevant,
        "reason": "未找到参考材料与 Sr/Ca 测量直接关联的证据" if relevant else "正文未识别到 Sr/Ca",
    }
    for sentence in re.split(r"(?<=[.!?。；])\s+", text):
        reference = re.search(standard, sentence, re.I)
        if not reference or not re.search(indicator, sentence, re.I):
            continue
        if re.search(r"\b(?:not|never)\s+(?:use[ds]?|measure[ds]?|calibrat\w+|correct\w+)", sentence, re.I):
            continue
        if not re.search(r"measur|monitor|reproducib|precision|accuracy|calibrat|correct|normaliz|测量|校准|监测", sentence, re.I):
            continue
        correction = bool(re.search(
            r"(?:Sr\s*/\s*Ca|锶钙比).{0,60}(?:corrected|normalized|校正).{0,100}JC[pt]\s*[-–]?\s*1",
            sentence, re.I,
        ))
        result.update(
            reference_reported=True,
            is_calibrated=correction,
            calibration_standard=re.sub(r"[\s–-]", "", reference.group()).upper()[:-1] + "-1",
            related_text=sentence,
            reference_unconfirmed=False,
            reason="正文报告了 Sr/Ca 参考材料校正" if correction else "正文报告了与 Sr/Ca 测量相关的参考材料",
        )
        break
    logging.info("Sr/Ca 参考材料检查: %s", result["reason"])
    return result
