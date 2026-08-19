#!/usr/bin/env python3
"""Reclean and summarize all event titles in the CalDAV calendar."""
from __future__ import annotations

import re
import requests
import json
import sqlite3
from pathlib import Path

CALDAV_URL = "http://127.0.0.1:5232/sanel/assignments/"
AUTH = ("sanel", "choose-a-strong-password")


def clean_title(title: str, course: str = "") -> str:
    t = title.strip()
    # Strip HTML and entity codes
    t = re.sub(r"x26#[a-z0-9]+;", "", t)
    t = re.sub(r"&[a-z]+;", "", t)
    
    # Strip day of week & date prefixes
    t = re.sub(r"^(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)[a-z/&, ]*\d{1,2}(?:[./-]\d{1,2})?\s*[-:—|]\s*)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^(?:Week\s+of\s+[A-Za-z0-9\s/-]+\s*[-:—|]\s*)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^(?:Day\s*\d+[A-Za-z0-9\s/-]*\s*[-:—|]\s*)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^(?:Success\s+Criteria\s*[-:—|]\s*)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^(?:Next\s+Week:\s*)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^\d+\)\s*", "", t)
    t = re.sub(r"^\d+\.\d+\s*", "", t)
    t = re.sub(r"^[|:]\s*", "", t)
    
    if "|" in t:
        parts = [p.strip() for p in t.split("|") if p.strip()]
        best = parts[0]
        for p in parts:
            if any(k in p.lower() for k in ["quiz", "test", "exam", "lab", "report", "homework", "due", "summative", "formative", "activity", "review"]):
                best = p
                break
        t = best

    t = re.sub(r"^\d+\)\s*", "", t)
    t = re.sub(r"^[|:-]\s*", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    
    # Specific cleanups
    if "canvas homework formative quiz" in t.lower():
        return "Canvas Homework Formative Quiz"
    if "toothpickase" in t.lower():
        return "Toothpickase Dry Lab"
    if "macromolecule" in t.lower():
        return "Macromolecule Structure & Function"
    if "water properties" in t.lower():
        return "Water Properties Review"
    if "protein folding" in t.lower():
        return "Protein Folding Lab Activity"
    if "line of reasoning" in t.lower() or "lor quiz" in t.lower():
        return "Line of Reasoning (LOR) Quiz"
    if "madman" in t.lower():
        return "Professor and the Madman Summative"
    if "fermentation" in t.lower():
        return "Fermentation Lab"
    if "safety" in t.lower():
        return "Lab Safety Assignment & Quiz"
    if "sem" in t.lower() and "1.10" in t.lower():
        return "SEM (Lesson 1.10) Practice"

    if len(t) > 42:
        t = t[:42].rsplit(" ", 1)[0].rstrip(" ,.-:")
    
    return t or "Course Assignment"


def main():
    print("Connecting to Radicale CalDAV...")
    resp = requests.request("PROPFIND", CALDAV_URL, auth=AUTH, headers={"Depth": "1"}, timeout=10)
    hrefs = [h for h in re.findall(r"<[^>]*href[^>]*>([^<]+)</[^>]*href[^>]*>", resp.text) if h.endswith(".ics")]
    print(f"Found {len(hrefs)} events to summarize & clean.")

    updated = 0
    for h in hrefs:
        url = "http://127.0.0.1:5232" + h
        ics_resp = requests.get(url, auth=AUTH)
        if ics_resp.status_code != 200:
            continue
        ics_text = ics_resp.text
        
        summary_m = re.search(r"SUMMARY:([^\r\n]+)", ics_text)
        cat_m = re.search(r"CATEGORIES:([^\r\n]+)", ics_text)
        
        if not summary_m:
            continue
        
        old_title = summary_m.group(1)
        course = cat_m.group(1) if cat_m else ""
        new_title = clean_title(old_title, course)
        
        if new_title != old_title:
            new_ics = ics_text.replace(f"SUMMARY:{old_title}", f"SUMMARY:{new_title}")
            put_resp = requests.put(url, auth=AUTH, data=new_ics.encode("utf-8"), headers={"Content-Type": "text/calendar; charset=utf-8"})
            if put_resp.status_code in {200, 201, 204}:
                print(f"  ✓ [{course[:15]}] '{old_title[:35]}...' -> '{new_title}'")
                updated += 1

    print(f"\n✓ Successfully cleaned and summarized {updated} calendar events!")


if __name__ == "__main__":
    main()
