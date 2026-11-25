"""Local index of saved BTW branches, separate from Codex conversation storage."""

from contextlib import contextmanager
import json
import msvcrt
from pathlib import Path


INDEX_PATH = Path(__file__).with_name(".btw-state.json")


def read_index():
    try:
        content = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(content, dict) or any(
            not isinstance(row, dict) or any(not isinstance(value, str) for value in row.values())
            for row in content.values()):
        raise ValueError("BTW 本地索引格式无效")
    return content


@contextmanager
def edit_index():
    with INDEX_PATH.with_suffix(".lock").open("a+b") as lock:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            index = read_index()
            yield index
            temp = INDEX_PATH.with_suffix(".tmp")
            temp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
            temp.replace(INDEX_PATH)
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
