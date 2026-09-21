"""Choosing WHICH collection day to work on (--daily / --day, GUI picker).

An upload can hold several YYYYMMDD folders, each with its own Daily file,
while Images/, GoCatorData/ and SBGData/ hold every day's runs together. Until
2026-09-18 the app globbed for a Daily file and took the first, so ten days in
a folder meant day one was processed and the other nine vanished from
run_mapping.csv with nothing said.

The rule these tests hold down: told a day, use exactly that day; told
nothing, behave exactly as before.
"""
from pathlib import Path

import cli
from core import discovery as disc
from core.daily import (daily_file_for_day, day_of_daily_file, find_daily_file,
                        list_daily_files)

DAILY_HEADER = "FILENAME,HEADER,Direction,From,To,LatBeg,LongBeg,Speed,Status\n"


def _day(root: Path, day: str, rows: list[str]) -> Path:
    """One day folder with its own Daily file."""
    folder = root / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"Daily_ARAN104_{day}.csv"
    path.write_text(DAILY_HEADER + "".join(rows), encoding="utf-8-sig")
    return path


def _row(stamp: str, section: str = "200180", status: str = "C") -> str:
    return (f"Video [{stamp}] run,{section},North,0.000,0.700,"
            f"43.436300,-80.315200,20,{status}\n")


def _three_days(root: Path) -> dict[str, Path]:
    return {
        "20260821": _day(root, "20260821", [_row("20260821.133437")]),
        "20260822": _day(root, "20260822", [_row("20260822.090000")]),
        "20260823": _day(root, "20260823", [_row("20260823.101500")]),
    }


class TestFindingTheDays:
    def test_lists_every_day_oldest_first(self, tmp_path):
        _three_days(tmp_path)
        assert [p.parent.name for p in list_daily_files(tmp_path)] == [
            "20260821", "20260822", "20260823"]

    def test_one_day_collection_is_unchanged(self, tmp_path):
        only = _day(tmp_path, "20260821", [_row("20260821.133437")])
        assert list_daily_files(tmp_path) == [only]
        assert find_daily_file(tmp_path) == only

    def test_no_daily_file_at_all(self, tmp_path):
        assert list_daily_files(tmp_path) == []
        assert find_daily_file(tmp_path) is None

    def test_day_is_read_from_the_name(self, tmp_path):
        days = _three_days(tmp_path)
        assert day_of_daily_file(days["20260822"]) == "20260822"

    def test_day_lookup_finds_it_and_says_no_when_absent(self, tmp_path):
        days = _three_days(tmp_path)
        assert daily_file_for_day(tmp_path, "20260823") == days["20260823"]
        assert daily_file_for_day(tmp_path, "20261231") is None


class TestChoosingOne:
    def test_no_choice_still_takes_the_oldest(self, tmp_path):
        """The old behaviour, kept on purpose: running by hand must not
        change. The CLI warns about it separately."""
        days = _three_days(tmp_path)
        assert find_daily_file(tmp_path) == days["20260821"]

    def test_a_chosen_day_is_used_exactly(self, tmp_path):
        days = _three_days(tmp_path)
        assert find_daily_file(tmp_path, days["20260823"]) == days["20260823"]

    def test_registered_stamps_are_that_day_only(self, tmp_path):
        """The whole point. Day two's Daily file must not register day one's
        or day three's runs."""
        days = _three_days(tmp_path)
        assert disc.daily_stamps(tmp_path, days["20260822"]) == {
            "20260822.090000"}
        assert disc.daily_stamps(tmp_path) == {"20260821.133437"}

    def test_status_x_is_read_from_the_chosen_day(self, tmp_path):
        """excluded_stamps has its own reader; it must follow the same day, or
        day one's false starts would be applied to day two's runs."""
        _day(tmp_path, "20260821", [_row("20260821.133437")])
        two = _day(tmp_path, "20260822",
                   [_row("20260822.090000", status="x"),
                    _row("20260822.093000")])
        assert disc.excluded_stamps(tmp_path, two) == {"20260822.090000"}
        assert disc.daily_stamps(tmp_path, two) == {"20260822.093000"}
        # nothing of day two leaks into the default (oldest) view
        assert disc.excluded_stamps(tmp_path) == set()


class TestTheCommandLine:
    def test_daily_that_is_not_there_exits_2(self, tmp_path, capsys):
        _three_days(tmp_path)
        code = cli.main(["process", str(tmp_path),
                         "--daily", str(tmp_path / "nope.csv")])
        assert code == cli.EXIT_CANNOT_RUN
        assert "no such file" in capsys.readouterr().err

    def test_unknown_day_exits_2_and_names_the_days_present(self, tmp_path,
                                                            capsys):
        _three_days(tmp_path)
        code = cli.main(["runs", str(tmp_path), "--day", "20260901"])
        assert code == cli.EXIT_CANNOT_RUN
        err = capsys.readouterr().err
        assert "20260821" in err and "20260823" in err

    def test_both_options_at_once_exits_2(self, tmp_path, capsys):
        days = _three_days(tmp_path)
        code = cli.main(["runs", str(tmp_path),
                         "--daily", str(days["20260821"]),
                         "--day", "20260821"])
        assert code == cli.EXIT_CANNOT_RUN
        assert "not both" in capsys.readouterr().err

    def test_a_missing_collection_folder_still_exits_2(self, tmp_path, capsys):
        code = cli.main(["runs", str(tmp_path / "not-here")])
        assert code == cli.EXIT_CANNOT_RUN

    def test_several_days_and_no_choice_warns(self, tmp_path, capsys):
        """A person running this by hand gets told, rather than quietly
        getting day one."""
        _three_days(tmp_path)
        cli.main(["runs", str(tmp_path)])
        err = capsys.readouterr().err
        assert "3 collection days" in err
        assert "NOT processed" in err

    def test_one_day_says_nothing(self, tmp_path, capsys):
        _day(tmp_path, "20260821", [_row("20260821.133437")])
        cli.main(["runs", str(tmp_path)])
        assert "collection days here" not in capsys.readouterr().err


class TestTheReportSaysWhichDay:
    def test_named_only_when_a_day_was_chosen(self):
        from core.batch import BatchReport

        plain = BatchReport(root="r", started_utc="2026-09-18T00:00:00+00:00")
        assert "daily:" not in plain.to_text()
        chosen = BatchReport(root="r", started_utc="2026-09-18T00:00:00+00:00",
                             daily_path=r"C:\c\20260822\Daily_ARAN104_20260822.csv")
        assert "daily:   C:\\c\\20260822\\Daily_ARAN104_20260822.csv" in chosen.to_text()
