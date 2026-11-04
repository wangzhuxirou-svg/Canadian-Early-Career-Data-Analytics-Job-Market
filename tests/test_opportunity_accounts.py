from pathlib import Path

import pytest
from openpyxl import Workbook

from src.opportunities.accounts import import_organizers_xlsx


def write_workbook(path: Path, rows: list[list[object]]) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["UTSC Club Accounts"])
    worksheet.append(["Instagram 账号名称", "社团名称", "Instagram 主页链接"])
    for row in rows:
        worksheet.append(row)
    workbook.save(path)


def test_imports_target_accounts_without_claiming_access_verification(tmp_path):
    source = tmp_path / "accounts.xlsx"
    write_workbook(source, [
        ["@data.club", "Data Club", "https://www.instagram.com/data.club/"],
        ["@finance_club", "Finance Club", "https://www.instagram.com/finance_club/"],
        ["Notes: target accounts were selected for collection", None, None],
    ])

    records = import_organizers_xlsx(source)

    assert [record["organizer_id"] for record in records] == ["data_club", "finance_club"]
    assert [record["priority"] for record in records] == [1, 2]
    assert all(record["enabled"] for record in records)
    assert all(record["last_verified_at"] is None for record in records)


def test_import_rejects_missing_required_columns(tmp_path):
    source = tmp_path / "accounts.xlsx"
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["Instagram 账号名称", "社团名称"])
    worksheet.append(["@data.club", "Data Club"])
    workbook.save(source)

    with pytest.raises(ValueError, match="missing required columns"):
        import_organizers_xlsx(source)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            [
                ["@invalid-name", "Club", "https://www.instagram.com/invalid-name/"],
            ],
            "invalid Instagram username",
        ),
        (
            [
                ["@same.club", "Club A", "https://www.instagram.com/same.club/"],
                ["@same.club", "Club B", "https://www.instagram.com/same.club/"],
            ],
            "duplicate username",
        ),
        (
            [["@data.club", "Data Club", "https://www.instagram.com/other.club/"]],
            "does not match username",
        ),
    ],
)
def test_import_rejects_invalid_or_duplicate_accounts(tmp_path, rows, message):
    source = tmp_path / "accounts.xlsx"
    write_workbook(source, rows)

    with pytest.raises(ValueError, match=message):
        import_organizers_xlsx(source)
