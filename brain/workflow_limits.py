from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowLimits:
    max_correction_iterations: int = 2
    max_modified_files: int = 5
    max_inspected_files: int = 20
    max_read_bytes_per_file: int = 256 * 1024
    max_total_change_bytes: int = 256 * 1024
    max_changed_lines: int = 500
    max_new_file_bytes: int = 64 * 1024
    focused_test_timeout: int = 60
    full_test_timeout: int = 180
    max_repeated_failure: int = 2

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} debe ser un entero")
            if value <= 0:
                raise ValueError(f"{name} debe ser mayor que cero")
