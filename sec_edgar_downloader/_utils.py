"""Utility functions for the downloader class."""

import time
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from typing import List
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ._constants import (
    DATE_FORMAT_TOKENS,
    ROOT_SAVE_FOLDER_NAME,
    SEC_EDGAR_ARCHIVES_BASE_URL,
    SEC_EDGAR_SEARCH_API_ENDPOINT,
)


class EdgarSearchApiError(Exception):
    """Error raised when Edgar Search API encounters a problem."""


# Object for storing metadata about filings that will be downloaded."""
FilingMetadata = namedtuple(
    "FilingMetaData",
    [
        "accession_number",
        "full_submission_filename",
        "full_submission_url",
        "filing_details_url",
        "filing_details_filename",
    ],
)


def validate_date_format(date_format: str) -> None:
    try:
        datetime.strptime(date_format, DATE_FORMAT_TOKENS)
    except ValueError:
        raise ValueError(
            "Incorrect date format. Please enter a date string of the form YYYY-MM-DD."
        )


def form_request_payload(
    ticker_or_cik: str,
    filing_types: List[str],
    start_date: str,
    end_date: str,
    start_index: int,
) -> dict:
    payload = {
        "dateRange": "custom",
        "startdt": start_date,
        "enddt": end_date,
        "category": "all",
        "locationType": "located",
        "locationCode": "all",
        "entityName": ticker_or_cik,
        "forms": filing_types,
        "from": start_index,
    }
    return payload


def build_filing_metadata_from_hit(hit: dict) -> FilingMetadata:
    accession_number, filing_details_filename = hit["_id"].split(":", 1)
    # Company CIK should be last in the CIK list. This list may also include
    # the CIKs of executives carrying out insider transactions like in form 4.
    cik = hit["_source"]["ciks"][-1]
    accession_number_no_dashes = accession_number.replace("-", "", 2)

    # TODO: add support for downloading original XML
    #  and HTML files using filing_details_filename
    submission_base_url = (
        f"{SEC_EDGAR_ARCHIVES_BASE_URL}/{cik}/{accession_number_no_dashes}"
    )

    full_submission_filename = f"{accession_number}.txt"
    full_submission_url = f"{submission_base_url}/{full_submission_filename}"

    # Get XSL if human readable is wanted
    # XSL is required to download the human-readable
    # and styled version of XML documents like form 4
    # SEC_EDGAR_ARCHIVES_BASE_URL + /320193/000032019320000066/wf-form4_159839550969947.xml
    # SEC_EDGAR_ARCHIVES_BASE_URL +
    #           /320193/000032019320000066/xslF345X03/wf-form4_159839550969947.xml

    # xsl = hit["_source"]["xsl"]
    # if xsl is not None:
    #     filing_details_url = f"{submission_base_url}/{xsl}/{filing_details_filename}"
    # else:
    #     filing_details_url = f"{submission_base_url}/{filing_details_filename}"

    filing_details_url = f"{submission_base_url}/{filing_details_filename}"

    return FilingMetadata(
        accession_number=accession_number,
        full_submission_url=full_submission_url,
        full_submission_filename=full_submission_filename,
        filing_details_url=filing_details_url,
        filing_details_filename=filing_details_filename,
    )


# TODO: add support for filing type lists
def get_filing_urls_to_download(
    filing_type: str,
    ticker_or_cik: str,
    num_filings_to_download: int,
    after_date: str,
    before_date: str,
    include_amends: bool,
) -> List[FilingMetadata]:
    filings_to_fetch = []
    start_index = 0

    while len(filings_to_fetch) < num_filings_to_download:
        payload = form_request_payload(
            ticker_or_cik, [filing_type], after_date, before_date, start_index
        )
        resp = requests.post(SEC_EDGAR_SEARCH_API_ENDPOINT, json=payload)
        resp.raise_for_status()
        search_query_results = resp.json()

        if "error" in search_query_results:
            try:
                error_reason = search_query_results["error"]["root_cause"]["reason"]
                raise EdgarSearchApiError(
                    f"Edgar Search API encountered an error: {error_reason}. "
                    f"Request payload: {payload}"
                )
            except KeyError:
                raise EdgarSearchApiError(
                    "Edgar Search API encountered an unknown error."
                    f"Request payload: {payload}"
                )

        query_hits = search_query_results["hits"]["hits"]

        # No more results to process
        if not query_hits:
            break

        for hit in query_hits:
            hit_filing_type = hit["_source"]["file_type"]

            is_amend = hit_filing_type[-2:] == "/A"
            if not include_amends and is_amend:
                continue

            # Work around bug where incorrect filings are sometimes included.
            # For example, AAPL 8-K searches include N-Q entries.
            if not is_amend and hit_filing_type != filing_type:
                continue

            metadata = build_filing_metadata_from_hit(hit)
            filings_to_fetch.append(metadata)

            if len(filings_to_fetch) == num_filings_to_download:
                return filings_to_fetch

        # Edgar queries 100 entries at a time, but it is best to set this
        # from the response payload in case it changes in the future
        query_size = search_query_results["query"]["size"]
        start_index += query_size

    return filings_to_fetch


def resolve_relative_urls_in_filing(filing_text: str, base_url: str) -> str:
    soup = BeautifulSoup(filing_text, "html.parser")

    for url in soup.find_all("a", href=True):
        url["href"] = urljoin(base_url, url["href"])

    for image in soup.find_all("img", src=True):
        image["src"] = urljoin(base_url, image["src"])

    return str(soup)


def download_and_save_filing(
    download_folder: Path,
    ticker_or_cik: str,
    filing_type: str,
    download_url: str,
    save_filename: str,
    resolve_urls: bool = False,
) -> None:
    resp = requests.get(download_url)
    resp.raise_for_status()
    filing_text = resp.text

    # Only resolve URLs in HTML files
    if resolve_urls and Path(save_filename).suffix in [".htm", ".html"]:
        base_url = f"{download_url.rsplit('/', 1)[0]}/"
        filing_text = resolve_relative_urls_in_filing(filing_text, base_url)

    # Create all parent directories as needed and write content to file
    save_path = download_folder.joinpath(
        ROOT_SAVE_FOLDER_NAME, ticker_or_cik, filing_type, save_filename
    )
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_text(filing_text, encoding="utf-8")


def download_filings(
    download_folder: Path,
    ticker_or_cik: str,
    filing_type: str,
    filings_to_fetch: List[FilingMetadata],
) -> None:
    for filing in filings_to_fetch:
        download_and_save_filing(
            download_folder,
            ticker_or_cik,
            filing_type,
            filing.full_submission_url,
            filing.full_submission_filename,
        )

        # SEC limits users to no more than 10 downloads per second
        # Sleep >0.10s between each download to prevent rate-limiting
        # Source: https://www.sec.gov/developer
        time.sleep(0.12)

        # download_and_save_filing(
        #     filing.filing_details_url,
        #     filing.filing_details_filename,
        #     resolve_urls=True
        # )
        # time.sleep(0.12)
