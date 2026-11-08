"""Valid long history/summary basenames must still publish atomically (local fix over 65c0a3d).

Copied from the parent review's test_atomic_name_boundary.py; it now imports the production module instead of
loading the file by path. Published 65c0a3d fails the 233/248/multibyte cases with ENAMETOOLONG (old data kept).
"""
import pytest

from src import atomic_file


@pytest.mark.parametrize('name', ['a' * 35 + '.json', 'a' * 232 + '.json', 'a' * 233 + '.json',
                                  'a' * 248 + '.json', '語' * 82 + '.json'])
def test_valid_target_name_can_be_published(tmp_path, name):
    target = tmp_path / name
    target.write_text('previous memory', encoding='utf-8')
    atomic_file.write_text_atomically(str(target), 'complete new memory')
    assert target.read_text(encoding='utf-8') == 'complete new memory'
    assert list(tmp_path.iterdir()) == [target]
