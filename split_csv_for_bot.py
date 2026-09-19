#!/usr/bin/env python3
"""Split a large CSV into Telegram-safe parts without breaking CSV records.

The splitter works on raw bytes, so it does not load the whole file into RAM and
preserves the original encoding and line endings. It recognizes quoted fields,
escaped double quotes, CRLF/LF line endings, and records containing newlines.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


def safe_stem(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return value or "data"


def record_stream(source, delimiter: int = ord(","), chunk_size: int = 1024 * 1024):
    """Yield complete raw CSV records, including their original line terminator.

    This is a byte-level CSV boundary scanner. It treats a newline as a record
    boundary only when it is outside a quoted field. A quote followed by another
    quote is an escaped quote and does not close the field.
    """
    record = bytearray()
    in_quotes = False
    pending_quote = False

    while True:
        chunk = source.read(chunk_size)
        if not chunk:
            break
        index = 0
        while index < len(chunk):
            byte = chunk[index]
            record.append(byte)

            if pending_quote:
                # The quote before this byte was either an escaped quote or the
                # closing quote. A second quote means escaped; any other byte means
                # the field was closed and this byte is processed outside quotes.
                if byte == ord('"'):
                    pending_quote = False
                    index += 1
                    continue
                in_quotes = False
                pending_quote = False
                if byte in (10, 13):
                    # Continue below as an outside-quote newline.
                    pass

            elif byte == ord('"'):
                if in_quotes:
                    pending_quote = True
                else:
                    in_quotes = True

            if not in_quotes and not pending_quote and byte == 10:
                yield bytes(record)
                record.clear()
            index += 1

    if pending_quote:
        # A quote at EOF closes a valid quoted field.
        pending_quote = False
        in_quotes = False
    if record:
        yield bytes(record)
    if in_quotes:
        raise ValueError("Input ended inside a quoted CSV field; the source appears malformed.")


def split_csv(
    input_path: Path,
    output_directory: Path,
    max_bytes: int,
    *,
    has_header: bool,
    prefix: str | None,
) -> list[Path]:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    output_directory.mkdir(parents=True, exist_ok=True)
    output_prefix = safe_stem(prefix or input_path.stem)
    created: list[Path] = []
    part_number = 0
    current_size = 0
    output = None
    header: bytes | None = None

    def open_part() -> None:
        nonlocal output, part_number, current_size
        if output is not None:
            output.close()
        part_number += 1
        path = output_directory / f"{output_prefix}_part_{part_number:03d}.csv"
        output = path.open("wb")
        created.append(path)
        current_size = 0
        if header:
            if len(header) > max_bytes:
                raise ValueError("The header itself is larger than the requested part size.")
            output.write(header)
            current_size = len(header)
        print(f"Creating {path}")

    try:
        with input_path.open("rb") as source:
            for record_number, record in enumerate(record_stream(source), start=1):
                if has_header and record_number == 1:
                    header = record
                    continue
                if len(record) > max_bytes - (len(header) if header else 0):
                    raise ValueError(
                        f"CSV record {record_number} is too large to fit in one part: "
                        f"{len(record):,} bytes."
                    )
                if output is None or current_size + len(record) > max_bytes:
                    open_part()
                output.write(record)
                current_size += len(record)
    finally:
        if output is not None:
            output.close()

    if not created:
        # An empty file or header-only file still produces one valid part.
        open_part()
        if output is not None:
            output.close()
    return created


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Split a large CSV at complete CSV-record boundaries for Telegram uploads."
    )
    parser.add_argument("input_file", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument(
        "--max-mb", type=float, default=19.9,
        help="Maximum decimal megabytes per part (default: 19.9). Use 19.0 for extra margin.",
    )
    parser.add_argument("--has-header", action="store_true", help="Repeat the first CSV record in every part.")
    parser.add_argument("--prefix", help="Output filename prefix; defaults to the input stem.")
    args = parser.parse_args()

    if not args.input_file.is_file():
        print(f"Input file not found: {args.input_file}", file=sys.stderr)
        return 2
    if args.max_mb <= 0:
        print("--max-mb must be positive", file=sys.stderr)
        return 2

    max_bytes = int(args.max_mb * 1_000_000)
    try:
        parts = split_csv(
            args.input_file,
            args.output_directory,
            max_bytes,
            has_header=args.has_header,
            prefix=args.prefix,
        )
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Done: {len(parts)} part(s), maximum size {max_bytes:,} bytes each.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
