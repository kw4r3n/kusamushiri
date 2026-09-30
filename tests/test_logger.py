import logging
from collections.abc import Iterator

import pytest

from kusamushiri import logger as logger_module


@pytest.fixture
def isolated_logger_name() -> Iterator[str]:
    name = "XDeleterTestNoFileLogging"
    test_logger = logging.getLogger(name)
    for handler in list(test_logger.handlers):
        test_logger.removeHandler(handler)
        handler.close()
    yield name
    for handler in list(test_logger.handlers):
        test_logger.removeHandler(handler)
        handler.close()


def test_setup_logger_continues_when_file_logging_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    isolated_logger_name: str,
) -> None:
    class FailingRotatingFileHandler:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise OSError("read-only log directory")

    monkeypatch.setattr(logger_module, "RotatingFileHandler", FailingRotatingFileHandler)

    test_logger = logger_module.setup_logger(isolated_logger_name)

    assert len(test_logger.handlers) == 1
    assert isinstance(test_logger.handlers[0], logging.StreamHandler)
