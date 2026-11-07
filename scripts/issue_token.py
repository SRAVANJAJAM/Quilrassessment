"""Issue a short-lived token for the local MCP walkthrough."""

import argparse

from quilr_assessment.auth import issue_demo_token
from quilr_assessment.config import Settings

parser = argparse.ArgumentParser()
parser.add_argument("role", choices=("viewer", "admin"))
args = parser.parse_args()
print(issue_demo_token(args.role, Settings()))
