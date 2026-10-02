"""自定义异常体系（来源：自主规划插件 core/exceptions.py，照搬）。

异常分类：
    - 可恢复异常：网络超时、临时故障等（应重试）
    - 不可恢复异常：配额超限、权限错误等（不应重试）
"""


class AutonomousPlanningError(Exception):
    """自主规划插件基础异常类（所有插件特定异常的父类）。"""


# ============================================================================
# LLM 相关异常（用于日程生成）
# ============================================================================

class LLMError(AutonomousPlanningError):
    """LLM 调用异常的基类。"""


class LLMQuotaExceededError(LLMError):
    """LLM 配额超限异常（不可恢复，不应重试）。"""

    def __init__(self, message: str, quota_type: str = "unknown"):
        super().__init__(message)
        self.quota_type = quota_type


class LLMTimeoutError(LLMError):
    """LLM 调用超时异常（可恢复，可以重试）。"""

    def __init__(self, message: str, timeout_seconds: float = None):
        super().__init__(message)
        self.timeout_seconds = timeout_seconds


class LLMInvalidResponseError(LLMError):
    """LLM 响应格式无效异常（JSON 无法解析或不符合 schema；可用不同 prompt 重试）。"""

    def __init__(self, message: str, response: str = None):
        super().__init__(message)
        self.response = response


class LLMRateLimitError(LLMError):
    """LLM 速率限制异常（可恢复，应等待后重试）。"""

    def __init__(self, message: str, retry_after_seconds: float = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


# ============================================================================
# 数据库相关异常
# ============================================================================

class DatabaseError(AutonomousPlanningError):
    """数据库操作异常的基类。"""


class ValidationError(AutonomousPlanningError):
    """输入验证异常的基类。"""


class InvalidParametersError(ValidationError):
    """参数验证失败异常（goal 的 parameters 不符合预期 schema）。"""

    def __init__(self, message: str, field_name: str = None, invalid_value=None):
        super().__init__(message)
        self.field_name = field_name
        self.invalid_value = invalid_value


class InvalidTimeWindowError(ValidationError):
    """时间窗口无效异常（time_window 格式不正确或范围无效）。"""

    def __init__(self, message: str, time_window=None):
        super().__init__(message)
        self.time_window = time_window


# ============================================================================
# 调度相关异常
# ============================================================================

class ScheduleError(AutonomousPlanningError):
    """日程生成异常的基类。"""


class ScheduleGenerationError(ScheduleError):
    """日程生成失败异常（重试后仍然失败）。"""

    def __init__(self, message: str, attempt_count: int = 0):
        super().__init__(message)
        self.attempt_count = attempt_count
