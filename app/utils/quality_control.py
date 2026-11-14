"""
【已弃用】此模块已弃用，请使用 app.core.moa.quality_control.QualityControlModule

此文件将在未来版本中移除。
如需异常值检测功能，请使用：
    from app.core.moa.quality_control import QualityControlModule
    
    qc_module = QualityControlModule()
    # 使用 check_data_quality 方法进行数据质量检查
"""

import warnings
warnings.warn(
    "app.utils.quality_control 已弃用，请使用 app.core.moa.quality_control.QualityControlModule",
    DeprecationWarning,
    stacklevel=2
)

def detect_outliers_long_term(data, holocene_stage):
    """扩展：按全新世时段调整异常值阈值"""
    import pandas as pd
    import numpy as np
    
    # 转换为pandas Series以便处理
    if not isinstance(data, pd.Series):
        data = pd.Series(data)
    
    if holocene_stage in ["early", "middle"]:
        # 早/中全新世：放宽阈值（IQR×2，Z-score＞4）
        q1 = data.quantile(0.25)
        q3 = data.quantile(0.75)
        iqr = q3 - q1
        lower_bound = q1 - 2 * iqr
        upper_bound = q3 + 2 * iqr
        # 滚动窗口用100年（适用于百年尺度数据）
        rolling_z = data.rolling(window=100, center=True, min_periods=1).apply(
            lambda x: abs((x.iloc[-1] - x.mean()) / x.std()) if len(x) > 1 and x.std() != 0 else 0
        )
        outliers = (data < lower_bound) | (data > upper_bound) | (rolling_z > 4)
    else:
        # 晚全新世/CE：沿用原标准（IQR×1.5，Z-score＞3）
        q1 = data.quantile(0.25)
        q3 = data.quantile(0.75)
        iqr = q3 - q1
        lower_bound = q1 - 1.5 * iqr
        upper_bound = q3 + 1.5 * iqr
        rolling_z = data.rolling(window=12, center=True, min_periods=1).apply(
            lambda x: abs((x.iloc[-1] - x.mean()) / x.std()) if len(x) > 1 and x.std() != 0 else 0
        )
        outliers = (data < lower_bound) | (data > upper_bound) | (rolling_z > 3)
    
    # 物理合理性校验（扩展早期范围）
    if data.name == "Sr/Ca":
        outliers = outliers | (data < 7.8) | (data > 10.2)
    elif data.name == "δ¹⁸O":
        outliers = outliers | (data < -3) | (data > 4)
    
    return outliers.astype(int)  # 1=异常值，0=正常
