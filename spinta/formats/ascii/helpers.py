from typing import List, Optional, Tuple


def get_widths(
    rows,
    cols: List[str],
    max_value_length: int = 100,
    max_col_width: Optional[int] = None,
    rows_to_check: int = 200
) -> Tuple[List[dict], dict]:
    widths = {}
    max_widths = {}
    max_split_widths = {}
    read_rows = []

    for i, row in enumerate(rows):
        read_rows.append(row)
        if i >= rows_to_check:
            break
        else:
            for col in cols:
                value = row.get(col)
                if value:
                    value = str(value)
                    if len(value) > max_value_length:
                        value = value[:max_value_length] + "..."

                    if '\n' in value:
                        value_width = max([len(part) for part in value.split('\n')])
                    else:
                        value_width = len(value)

                    width = max(value_width, len(col))
                    split_width = max(len(value.split(' ')[0]), len(col))
                else:
                    width = len(col)
                    split_width = len(col)

                if col not in max_widths or width > max_widths[col]:
                    max_widths[col] = width
                if col not in max_split_widths or split_width > max_split_widths[col]:
                    max_split_widths[col] = split_width

                if col not in widths:
                    widths[col] = sum([max_widths[col], max_split_widths[col]]) // 2
                else:
                    widths[col] = sum([widths[col], max_widths[col], max_split_widths[col]]) // 3

                if max_col_width and widths[col] > max_col_width:
                    widths[col] = max_col_width

    return read_rows, widths


def get_displayed_cols(
    widths: dict,
    max_width: int,
    separator: str = "  "
) -> List[str]:
    total_width = 0
    cols = []
    for col, width in widths.items():
        total_width += width + len(separator)
        if total_width > max_width:
            break
        else:
            cols.append(col)
    return cols


def draw_border(
    widths: dict,
    displayed_cols: List[str],
    separator: str = "  "
) -> str:
    result = ""
    for col, width in widths.items():
        border = '-' * width
        if col in displayed_cols:
            result += border + separator
        else:
            result += "..."
            break
    return result + "\n"


def draw_header(
    widths: dict,
    displayed_cols: List[str],
    separator: str = "  "
) -> str:
    result = ""
    for col, width in widths.items():
        if col in displayed_cols:
            if width > len(col):
                offset = width - len(col)
                col += ' ' * offset
            result += col + separator
        else:
            result += "..."
            break
    return result + "\n"


def draw_row(
    data: dict,
    widths: dict,
    displayed_cols: List[str],
    max_value_length: int = 100,
    separator: str = "  "
) -> str:
    lines = {}
    line_count = 1
    lines[line_count] = prepare_line(widths, displayed_cols)
    shortened = False

    for col, width in widths.items():
        if col not in displayed_cols:
            shortened = True
            break

        value = data.get(col)
        value = str(value) if value else "∅"
        line_count = 1

        if len(value) > max_value_length:
            value = value[:max_value_length] + "..."

        if '\n' in value:
            split_value = value.split('\n')
            for i, part in enumerate(split_value):
                part = part.strip()
                lines, line_count = get_row_lines(
                    col,
                    part,
                    width,
                    lines,
                    line_count,
                    widths,
                    displayed_cols
                )

                if i < (len(split_value) - 1):
                    line_count += 1
                    if line_count not in lines:
                        lines[line_count] = prepare_line(widths, displayed_cols)
        else:
            lines, _ = get_row_lines(
                col,
                value,
                width,
                lines,
                line_count,
                widths,
                displayed_cols
            )

    result = ""
    for i, row in enumerate(lines.values()):
        ending = ""
        if shortened:
            ending += separator + "..."
        if i < (len(lines.values()) - 1):
            ending += "\\"
        ending += '\n'
        result += separator.join(row.values()) + ending

    return result


def prepare_line(
    widths: dict,
    displayed_cols: List[str]
) -> dict:
    return {col: ' ' * widths[col] for col in widths.keys() if col in displayed_cols}


def get_row_lines(
    col: str,
    value: str,
    width: int,
    lines: dict,
    line_count: int,
    widths: dict,
    displayed_cols: List[str]
) -> Tuple[dict, int]:
    if len(value) > width:
        split_value = [value[i:i + width] for i in range(0, len(value), width)]
        for i, part in enumerate(split_value):
            part = part.strip()
            if width > len(part):
                offset = width - len(part)
                part += ' ' * offset
            lines[line_count][col] = part

            if i < (len(split_value) - 1):
                line_count += 1
                if line_count not in lines:
                    lines[line_count] = prepare_line(widths, displayed_cols)
    else:
        if width > len(value):
            offset = width - len(value)
            value += ' ' * offset

        lines[line_count][col] = value

    return lines, line_count
