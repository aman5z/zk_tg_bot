"""
report_builder.py
Build attendance reports in XLSX, PNG, and PDF formats.
Supports department-priority sorting and multiple visual templates.
"""

import logging
from datetime import date
from io import BytesIO
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use('Agg')  # non-interactive backend — must be before pyplot import
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill

logger = logging.getLogger(__name__)

# ─── Department sort order ────────────────────────────────────────────────────
# TEACHING first, then ADMIN, SUPPORT, DRIVER, CLEANING STAFF, then rest alpha.
DEPT_ORDER = [
    'TEACHING',
    'ADMIN',
    'SUPPORT',
    'DRIVER',
    'CLEANING STAFF',
]


def dept_sort_key(dept: str) -> tuple:
    upper = (dept or '').upper().strip()
    try:
        return (DEPT_ORDER.index(upper), dept)
    except ValueError:
        return (len(DEPT_ORDER), dept)


# ─── Visual templates ─────────────────────────────────────────────────────────
TEMPLATES: Dict[str, Dict] = {
    'default': {
        'label':       'Default (Blue)',
        'header_bg':   '#2196F3',
        'header_fg':   'white',
        'row_even':    '#E3F2FD',
        'row_odd':     '#FFFFFF',
        'border':      '#BBDEFB',
    },
    'dark': {
        'label':       'Dark (Navy)',
        'header_bg':   '#1A237E',
        'header_fg':   'white',
        'row_even':    '#E8EAF6',
        'row_odd':     '#FFFFFF',
        'border':      '#C5CAE9',
    },
    'green': {
        'label':       'Green (School)',
        'header_bg':   '#1B5E20',
        'header_fg':   'white',
        'row_even':    '#E8F5E9',
        'row_odd':     '#FFFFFF',
        'border':      '#A5D6A7',
    },
}

_COL_WIDTHS_XLS = {'A': 10, 'B': 36, 'C': 22}   # column letter → character width
_COL_WIDTHS_FIG = [0.09, 0.42, 0.22]             # relative widths for figure table


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _hex_to_rgb(h: str) -> Tuple[float, float, float]:
    h = h.lstrip('#')
    if len(h) != 6 or not all(c in '0123456789abcdefABCDEF' for c in h):
        raise ValueError(f"Invalid hex colour: '#{h}'")
    return tuple(int(h[i:i+2], 16) / 255.0 for i in (0, 2, 4))


def _sort_and_filter(
    absent: List[Dict],
    departments: str = 'ALL',
    extra_exclude_badges: Optional[set] = None,
) -> List[Dict]:
    """Filter by department and sort by DEPT_ORDER, then name."""
    result = absent
    if departments and departments.strip().upper() != 'ALL':
        selected = {d.strip().upper() for d in departments.split(',') if d.strip()}
        result = [e for e in result if (e.get('dept') or '').upper() in selected]
    if extra_exclude_badges:
        result = [e for e in result if e.get('badge') not in extra_exclude_badges]
    result = sorted(result, key=lambda e: (dept_sort_key(e.get('dept', '')),
                                           (e.get('name') or '').upper()))
    return result


def _to_rows(emp_list: List[Dict]) -> List[Dict]:
    return [{'Badge': e['badge'], 'Name': e['name'], 'Department': e['dept']}
            for e in emp_list]


# ─── XLSX ─────────────────────────────────────────────────────────────────────

def build_xlsx(rows: List[Dict], title: str = '', template: str = 'default') -> BytesIO:
    tpl = TEMPLATES.get(template, TEMPLATES['default'])
    df = pd.DataFrame(rows, columns=['Badge', 'Name', 'Department'])
    buf = BytesIO()

    with pd.ExcelWriter(buf, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Absent', startrow=1 if title else 0)
        ws = writer.sheets['Absent']

        header_row = 2 if title else 1  # 1-indexed row where Badge/Name/Dept header lives

        # Optional title row
        if title:
            ws.merge_cells('A1:C1')
            tc = ws['A1']
            tc.value = title
            tc.font = Font(bold=True, size=13, color='FFFFFF')
            tc.fill = PatternFill(
                start_color=tpl['header_bg'].lstrip('#'),
                end_color=tpl['header_bg'].lstrip('#'),
                fill_type='solid')
            tc.alignment = Alignment(horizontal='center', vertical='center')
            ws.row_dimensions[1].height = 22

        # Style the Badge / Name / Department header row
        hdr_fill = PatternFill(
            start_color=tpl['header_bg'].lstrip('#'),
            end_color=tpl['header_bg'].lstrip('#'),
            fill_type='solid')
        hdr_font = Font(bold=True, color='FFFFFF', size=11)
        for col in range(1, 4):
            cell = ws.cell(row=header_row, column=col)
            cell.fill = hdr_fill
            cell.font = hdr_font
            cell.alignment = Alignment(horizontal='center')

        # Alternating row fill
        even_fill = PatternFill(
            start_color=tpl['row_even'].lstrip('#'),
            end_color=tpl['row_even'].lstrip('#'),
            fill_type='solid')
        data_start = header_row + 1
        for row_idx in range(data_start, data_start + len(rows)):
            if (row_idx - data_start) % 2 == 0:
                for col in range(1, 4):
                    ws.cell(row=row_idx, column=col).fill = even_fill

        # Column widths
        for col_letter, width in _COL_WIDTHS_XLS.items():
            ws.column_dimensions[col_letter].width = width

    buf.seek(0)
    return buf


# ─── PNG ──────────────────────────────────────────────────────────────────────

def build_png(rows: List[Dict], title: str = '', template: str = 'default') -> BytesIO:
    tpl = TEMPLATES.get(template, TEMPLATES['default'])
    n = len(rows)

    fig_h = max(2.5, 0.95 + n * 0.38)
    fig, ax = plt.subplots(figsize=(10, fig_h))
    ax.set_facecolor('white')
    fig.patch.set_facecolor('white')
    ax.axis('off')

    if not rows:
        ax.text(0.5, 0.5, 'No absences today — All present!', ha='center', va='center',
                fontsize=14, transform=ax.transAxes)
    else:
        col_labels = ['Badge', 'Name', 'Department']
        cell_data = [[r.get('Badge', ''), r.get('Name', ''), r.get('Department', '')]
                     for r in rows]
        cell_colors = [
            [tpl['row_even'] if i % 2 == 0 else tpl['row_odd']] * 3
            for i in range(n)
        ]
        col_colors = [_hex_to_rgb(tpl['header_bg'])] * 3

        tbl = ax.table(
            cellText=cell_data,
            colLabels=col_labels,
            cellColours=cell_colors,
            colColours=col_colors,
            loc='upper center',
            cellLoc='left',
        )
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(9)

        # Style header text
        for j in range(3):
            tbl[0, j].set_text_props(color=tpl['header_fg'], fontweight='bold')

        # Column widths
        for j, w in enumerate(_COL_WIDTHS_FIG):
            for i in range(n + 1):
                tbl[i, j].set_width(w)

    if title:
        fig.suptitle(title, fontsize=11, fontweight='bold', y=0.99,
                     color='#333333')

    plt.tight_layout(rect=[0, 0, 1, 0.96 if title else 1.0])
    buf = BytesIO()
    fig.savefig(buf, format='png', dpi=150, bbox_inches='tight',
                facecolor='white', edgecolor='none')
    plt.close(fig)
    buf.seek(0)
    return buf


# ─── PDF ──────────────────────────────────────────────────────────────────────

_ROWS_PER_PAGE = 48


def build_pdf(rows: List[Dict], title: str = '', template: str = 'default') -> BytesIO:
    tpl = TEMPLATES.get(template, TEMPLATES['default'])
    # When rows is empty produce a single empty page; otherwise paginate.
    if rows:
        pages = [rows[i:i + _ROWS_PER_PAGE]
                 for i in range(0, len(rows), _ROWS_PER_PAGE)]
    else:
        pages = [[]]

    buf = BytesIO()
    with PdfPages(buf) as pdf:
        for page_idx, page_rows in enumerate(pages):
            n = len(page_rows)
            fig_h = max(3.0, 1.3 + n * 0.38)
            fig, ax = plt.subplots(figsize=(10, fig_h))
            ax.set_facecolor('white')
            fig.patch.set_facecolor('white')
            ax.axis('off')

            page_title = title
            if len(pages) > 1:
                page_title += f'  (page {page_idx + 1}/{len(pages)})'

            if page_rows:
                col_labels = ['Badge', 'Name', 'Department']
                cell_data = [[r.get('Badge', ''), r.get('Name', ''), r.get('Department', '')]
                             for r in page_rows]
                cell_colors = [
                    [tpl['row_even'] if i % 2 == 0 else tpl['row_odd']] * 3
                    for i in range(n)
                ]
                col_colors = [_hex_to_rgb(tpl['header_bg'])] * 3

                tbl = ax.table(
                    cellText=cell_data,
                    colLabels=col_labels,
                    cellColours=cell_colors,
                    colColours=col_colors,
                    loc='upper center',
                    cellLoc='left',
                )
                tbl.auto_set_font_size(False)
                tbl.set_fontsize(9)
                for j in range(3):
                    tbl[0, j].set_text_props(color=tpl['header_fg'], fontweight='bold')
                for j, w in enumerate(_COL_WIDTHS_FIG):
                    for i in range(n + 1):
                        tbl[i, j].set_width(w)
            else:
                ax.text(0.5, 0.5, 'No absences today — All present!', ha='center', va='center',
                        fontsize=14, transform=ax.transAxes)

            if page_title:
                fig.suptitle(page_title, fontsize=11, fontweight='bold',
                             y=0.99, color='#333333')

            plt.tight_layout(rect=[0, 0, 1, 0.96 if page_title else 1.0])
            pdf.savefig(fig, bbox_inches='tight', facecolor='white', edgecolor='none')
            plt.close(fig)

    buf.seek(0)
    return buf


# ─── Main entry point ─────────────────────────────────────────────────────────

def build_absent_report(
    absent: List[Dict],
    report_date: date,
    departments: str = 'ALL',
    formats: str = 'xlsx',
    template: str = 'default',
    extra_exclude_badges: Optional[set] = None,
) -> Dict[str, Tuple[BytesIO, str]]:
    """
    Build absent report in the requested format(s).

    Returns dict of  format_key → (BytesIO, filename).
    format_key values: 'xlsx', 'png', 'pdf'
    """
    emp_list = _sort_and_filter(absent, departments, extra_exclude_badges)
    rows = _to_rows(emp_list)

    date_label = report_date.strftime('%d %B %Y')
    date_file  = report_date.strftime('%d-%m-%Y')
    title = f"Absent Report — {date_label}"
    filename_base = f"{date_file} Attendance Report"

    fmt_set = {f.strip().lower() for f in formats.split(',')}
    send_all = 'all' in fmt_set

    result: Dict[str, Tuple[BytesIO, str]] = {}

    if send_all or 'xlsx' in fmt_set:
        result['xlsx'] = (build_xlsx(rows, title, template),
                          f'{filename_base}.xlsx')
    if send_all or 'png' in fmt_set:
        result['png'] = (build_png(rows, title, template),
                         f'{filename_base}.png')
    if send_all or 'pdf' in fmt_set:
        result['pdf'] = (build_pdf(rows, title, template),
                         f'{filename_base}.pdf')

    return result
