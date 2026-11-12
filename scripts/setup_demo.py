import argparse
import logging

from app.config import SheetsSettings
from app.sheets_service import SheetsService


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize AI CRM Google Sheets")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--empty", action="store_true", help="Initialize the CRM without demo rows")
    modes.add_argument("--replace-leads", action="store_true", help="Replace existing lead rows with demo data")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sheets = SheetsService(SheetsSettings())
    sheets.initialize()
    if not args.empty:
        if not args.replace_leads and sheets.has_lead_data():
            parser.error("Leads already contains data. Use --empty or --replace-leads.")
        sheets.seed_demo_data()
        logging.info("Seeded 25 demo leads, including 4 unscored leads")
    sheets.build_dashboard()
    logging.info("CRM worksheets initialized")


if __name__ == "__main__":
    main()
