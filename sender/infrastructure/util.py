from __future__ import annotations

from typing import Any

import requests


def json_or_none(response: requests.Response) -> Any | None:
    try:
        return response.json()
    except ValueError:
        return None