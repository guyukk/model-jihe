"""
坐标转换模块 - 处理归一化坐标到机械坐标的转换
"""
import config


class CoordinateConverter:
    """坐标转换器"""

    def __init__(self):
        self.scale_factor = config.COORD_SCALE_FACTOR

    def normalized_to_mechanical(self, norm_x, norm_y):
        """
        将模型检测到的ROI归一化坐标转换为实际机械坐标。

        先按原坐标映射生成ROI局部机械坐标 (norm_y, norm_x)，再按当前
        标定要求交换两轴，并将ROI中点平移到机械坐标 (600, 1400)。
        坐标单位为0.1mm。

        Args:
            norm_x: 视觉归一化X坐标 (0-1)
            norm_y: 视觉归一化Y坐标 (0-1)

        Returns:
            (mech_x, mech_y): 实际机械坐标，单位0.1mm
        """
        # 保留检测管线原有的轴映射，再执行标定要求的X/Y互换。
        roi_mech_x = int(round(norm_y * self.scale_factor))
        roi_mech_y = int(round(norm_x * self.scale_factor))
        swapped_x, swapped_y = roi_mech_y, roi_mech_x

        mech_x = (swapped_x - config.COORD_ROI_CENTER
                  + config.COORD_MECHANICAL_CENTER_X)
        mech_y = (swapped_y - config.COORD_ROI_CENTER
                  + config.COORD_MECHANICAL_CENTER_Y)

        return mech_x, mech_y

    def mechanical_to_mm(self, mech_x, mech_y):
        """
        将机械坐标(0.1mm单位)转换为毫米

        Args:
            mech_x: 机械X坐标 (0.1mm单位)
            mech_y: 机械Y坐标 (0.1mm单位)

        Returns:
            (x_mm, y_mm): 坐标，单位mm
        """
        return mech_x / 10.0, mech_y / 10.0
