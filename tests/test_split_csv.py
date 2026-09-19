from pathlib import Path
import tempfile
import unittest

from split_csv_for_bot import split_csv


class SplitCsvTests(unittest.TestCase):
    def test_splits_at_records_and_preserves_multiline_quoted_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.csv"
            source.write_bytes(
                'id,name\n1,"first line\nsecond line"\n2,"a ""quoted"" value"\n'.encode("utf-8")
            )
            parts = split_csv(source, root / "parts", 35, has_header=True, prefix="source")
            self.assertGreaterEqual(len(parts), 2)
            combined = b"".join(
                part.read_bytes().replace(b"id,name\n", b"", 1) for part in parts
            )
            self.assertEqual(
                combined,
                b'1,"first line\nsecond line"\n2,"a ""quoted"" value"\n',
            )
            for part in parts:
                self.assertLessEqual(part.stat().st_size, 35)


if __name__ == "__main__":
    unittest.main()
