"""Google Sheets client.

All read / find-row / append-row / update-cell logic for both tabs lives here.
The spreadsheet is treated as the source of truth for vote values.

Layout expected in each tab (headers are read from row 1 at runtime, so the
exact column positions do not matter, only the header text):

    | Дата | <name column> | person | person | ... | Средняя оценка |

where ``<name column>`` is "Сервис" on the Partners tab and "Проект" on the
Projects tab. The "Средняя оценка" column holds an AVERAGE formula and is
never written to for existing rows.
"""

import logging
import re
import threading

import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger("qpoll.sheets")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_LEADING_NUMBER = re.compile(r"\d+")


class SheetColumnMissing(Exception):
    """A required column (usually a voter's column) is not in the tab header."""


def _is_transient(exc):
    """Retry only on rate-limit / 5xx responses from the Sheets API."""
    if isinstance(exc, gspread.exceptions.APIError):
        try:
            code = exc.response.status_code
        except Exception:
            return False
        return code in (429, 500, 502, 503, 504)
    return isinstance(exc, (ConnectionError, TimeoutError))


_retry = retry(
    retry=retry_if_exception(_is_transient),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
    before_sleep=lambda s: logger.warning(
        "Sheets API call failed (attempt %d), retrying in %.0fs: %s",
        s.attempt_number,
        s.next_action.sleep,
        s.outcome.exception(),
    ),
)


def extract_score(option_text):
    """"7 — Yaxshi" -> 7 ; returns None if no leading number is found."""
    m = _LEADING_NUMBER.search(option_text or "")
    return int(m.group(0)) if m else None


class SheetsClient:
    def __init__(self, sheet_id, cfg, credentials_path=None, credentials_info=None):
        """Either `credentials_path` (a service-account JSON file on disk) or
        `credentials_info` (the parsed JSON as a dict — handy on PaaS hosts
        where pasting the key into an env var is easier than shipping a file)
        must be given.
        """
        if credentials_info is not None:
            creds = Credentials.from_service_account_info(
                credentials_info, scopes=SCOPES
            )
        elif credentials_path is not None:
            creds = Credentials.from_service_account_file(
                credentials_path, scopes=SCOPES
            )
        else:
            raise ValueError("SheetsClient needs credentials_path or credentials_info")
        self._gc = gspread.authorize(creds)
        self._sheet_id = sheet_id
        self._sh = self._open()
        self._cfg = cfg
        self._s = cfg["sheets"]
        self._ws_cache = {}
        self._header_cache = {}
        # Serialise writes so two near-simultaneous poll answers can't both
        # decide a row is missing and append it twice.
        self._write_lock = threading.Lock()
        logger.info("Connected to spreadsheet '%s'", self._sh.title)

    # -- low level -----------------------------------------------------------

    @_retry
    def _open(self):
        return self._gc.open_by_key(self._sheet_id)

    @_retry
    def _worksheet(self, tab_name):
        ws = self._ws_cache.get(tab_name)
        if ws is None:
            ws = self._sh.worksheet(tab_name)
            self._ws_cache[tab_name] = ws
        return ws

    @_retry
    def _headers(self, tab_name, refresh=False):
        if refresh or tab_name not in self._header_cache:
            self._header_cache[tab_name] = self._worksheet(tab_name).row_values(1)
        return self._header_cache[tab_name]

    @_retry
    def _all_values(self, tab_name):
        return self._worksheet(tab_name).get_all_values()

    @_retry
    def _update_cell(self, tab_name, row, col, value):
        self._worksheet(tab_name).update_cell(row, col, value)

    @_retry
    def _append_row(self, tab_name, values):
        self._worksheet(tab_name).append_row(
            values, value_input_option="USER_ENTERED", table_range="A1"
        )

    @_retry
    def _draw_day_borders(self, tab_name, rows, width):
        """Solid black line on top of each given row (1-based), across the
        first ``width`` columns -- the separator the admin draws between days.
        Uses updateBorders so the cells' other edges are left untouched.
        """
        if not rows:
            return
        sheet_id = self._worksheet(tab_name).id
        requests = [
            {
                "updateBorders": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": r - 1,
                        "endRowIndex": r,
                        "startColumnIndex": 0,
                        "endColumnIndex": width,
                    },
                    "top": {
                        "style": "SOLID",
                        "color": {"red": 0, "green": 0, "blue": 0},
                    },
                }
            }
            for r in rows
        ]
        self._sh.batch_update({"requests": requests})

    # -- helpers -----------------------------------------------------------

    def _col_index(self, tab_name, header, required=True):
        headers = self._headers(tab_name)
        for i, h in enumerate(headers):
            if h.strip() == header.strip():
                return i + 1  # 1-based
        if not required:
            return None
        # one retry with a fresh header read in case the sheet changed
        headers = self._headers(tab_name, refresh=True)
        for i, h in enumerate(headers):
            if h.strip() == header.strip():
                return i + 1
        raise SheetColumnMissing(
            "Column %r not found in tab %r (headers: %s)"
            % (header, tab_name, headers)
        )

    def _find_row(self, tab_name, date_col, date_str, name_col, name):
        for idx, row in enumerate(self._all_values(tab_name), start=1):
            if idx == 1:
                continue
            a = row[date_col - 1] if len(row) >= date_col else ""
            b = row[name_col - 1] if len(row) >= name_col else ""
            if a.strip() == date_str and b.strip() == name:
                return idx
        return None

    def _create_row(self, tab_name, date_col, date_str, name_col, name):
        headers = self._headers(tab_name)
        width = len(headers)
        values = [""] * width
        values[date_col - 1] = date_str
        values[name_col - 1] = name

        avg_col = self._col_index(
            tab_name, self._s["average_header"], required=False
        )
        if self._s.get("add_average_formula_on_new_row") and avg_col:
            first_person = name_col + 1
            last_person = avg_col - 1
            if last_person >= first_person:
                new_row_number = len(self._all_values(tab_name)) + 1
                start = rowcol_to_a1(new_row_number, first_person)
                end = rowcol_to_a1(new_row_number, last_person)
                values[avg_col - 1] = "=IFERROR(AVERAGE(%s:%s),\"\")" % (start, end)

        self._append_row(tab_name, values)
        all_values = self._all_values(tab_name)
        row = self._find_row(tab_name, date_col, date_str, name_col, name)
        if row and row > 2:
            prev = all_values[row - 2]
            prev_date = prev[date_col - 1].strip() if len(prev) >= date_col else ""
            if prev_date != date_str:
                self._draw_day_borders(tab_name, [row], width)
        logger.info(
            "Created new row %s in %r for %s / %s", row, tab_name, date_str, name
        )
        return row

    # -- public ----------------------------------------------------------

    def record_score(self, tab_name, name_header, entity_name, date_str,
                     column_name, value):
        """Write ``value`` for ``column_name`` into the row keyed by
        (``date_str``, ``entity_name``) in ``tab_name``, creating the row if
        needed. ``value`` of ``None`` clears the cell (vote retracted).

        Returns the 1-based row number that was written.
        """
        with self._write_lock:
            date_col = self._col_index(tab_name, self._s["date_header"])
            name_col = self._col_index(tab_name, name_header)
            value_col = self._col_index(tab_name, column_name)  # may raise

            avg_col = self._col_index(
                tab_name, self._s["average_header"], required=False
            )
            if avg_col and value_col == avg_col:
                raise ValueError(
                    "Refusing to write into the '%s' column"
                    % self._s["average_header"]
                )

            row = self._find_row(tab_name, date_col, date_str, name_col,
                                 entity_name)
            if row is None:
                row = self._create_row(tab_name, date_col, date_str, name_col,
                                       entity_name)

            cell_value = "" if value is None else value
            self._update_cell(tab_name, row, value_col, cell_value)
            logger.info(
                "Sheet write: %r [%s / %s] %s (col %d) = %r (row %d)",
                tab_name, date_str, entity_name, column_name, value_col,
                cell_value, row,
            )
            return row

    def ensure_row(self, tab_name, name_header, entity_name, date_str):
        """Make sure a row for (date_str, entity_name) exists in tab_name,
        creating it (blank person-columns, an average formula if configured)
        when it doesn't -- without writing to any voter's cell. Used to
        proactively lay down today's rows for every partner/project when
        the polls are sent, instead of only on the first vote for that day.
        """
        with self._write_lock:
            date_col = self._col_index(tab_name, self._s["date_header"])
            name_col = self._col_index(tab_name, name_header)
            row = self._find_row(tab_name, date_col, date_str, name_col,
                                 entity_name)
            if row is None:
                row = self._create_row(tab_name, date_col, date_str, name_col,
                                       entity_name)
            return row

    def apply_day_borders(self, tab_name):
        """Draw the day-separator line above every row whose date differs
        from the row above it. Idempotent; run at startup to fix rows that
        were created before the bot drew these borders itself.
        """
        with self._write_lock:
            date_col = self._col_index(tab_name, self._s["date_header"])
            width = len(self._headers(tab_name))
            rows = []
            prev_date = None
            for idx, row in enumerate(self._all_values(tab_name), start=1):
                if idx == 1:
                    continue
                d = row[date_col - 1].strip() if len(row) >= date_col else ""
                if not d:
                    break
                if prev_date is not None and d != prev_date:
                    rows.append(idx)
                prev_date = d
            self._draw_day_borders(tab_name, rows, width)
            logger.info("Day borders applied in %r above rows %s", tab_name, rows)

    # -- poll_id -> metadata log (survives a redeploy on hosts with no -----
    # -- persistent disk, e.g. Render's free tier, unlike local SQLite) ----

    POLL_LOG_HEADERS = [
        "poll_id", "date", "poll_type", "chat_id", "message_id",
        "group_label", "project_name", "partner_key", "partner_sheet_name",
        "question", "closed",
    ]

    @_retry
    def _poll_log_ws(self):
        tab_name = self._s.get("poll_log_tab", "PollLog")
        ws = self._ws_cache.get(tab_name)
        if ws is not None:
            return ws
        try:
            ws = self._sh.worksheet(tab_name)
        except gspread.exceptions.WorksheetNotFound:
            ws = self._sh.add_worksheet(
                title=tab_name, rows=2000, cols=len(self.POLL_LOG_HEADERS)
            )
            ws.append_row(self.POLL_LOG_HEADERS, value_input_option="RAW")
            logger.info("Created missing %r tab for poll metadata", tab_name)
        self._ws_cache[tab_name] = ws
        return ws

    @_retry
    def load_poll_log(self):
        """All poll metadata ever recorded, as a list of dicts -- used once at
        startup to recover the poll_id -> metadata map after a restart that
        wiped local storage.
        """
        return self._poll_log_ws().get_all_records()

    @_retry
    def append_poll_log(self, meta):
        self._poll_log_ws().append_row(
            [
                str(meta["poll_id"]), meta["date"], meta["poll_type"],
                meta["chat_id"], meta["message_id"],
                meta.get("group_label") or "", meta.get("project_name") or "",
                meta.get("partner_key") or "", meta.get("partner_sheet_name") or "",
                meta.get("question") or "", 0,
            ],
            value_input_option="RAW",
        )

    @_retry
    def mark_poll_log_closed(self, poll_id):
        ws = self._poll_log_ws()
        closed_col = len(self.POLL_LOG_HEADERS)
        for i, row in enumerate(ws.get_all_values()[1:], start=2):
            if row and row[0] == str(poll_id):
                ws.update_cell(i, closed_col, 1)
                return
