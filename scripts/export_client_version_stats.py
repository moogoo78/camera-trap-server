"""
Export per-client_version upload stats to CSV.

New clients save client_version on DeploymentJournal; old clients (before
1.1.12) only wrote it into the first segment of Image.memo
(APP-v1.1.2/D150-TS026/1.1.2 (2023.06.20)/23).

Usage:
  python scripts/export_client_version_stats.py
  python scripts/export_client_version_stats.py --out stats.csv
  python scripts/export_client_version_stats.py --project 329
  python scripts/export_client_version_stats.py --exclude-prerelease
"""

import argparse
import csv
import os
import re
import sys
import django

# Django setup
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'conf.settings')
django.setup()

from django.db import connection
from django.db.models import Count, Sum
from taicat.models import DeploymentJournal


PRERELEASE_RE = re.compile(r'alpha|beta|rc\d*|dev|pre|snapshot', re.I)


def is_prerelease(version):
    # look only at what follows x.y.z, ex: 1.1.16-beta.1(2025.03.14), 1.2.0rc1
    m = re.search(r'\d+\.\d+\.\d+(.*)', version or '')
    return bool(m and PRERELEASE_RE.search(m.group(1).split('(')[0]))


def journal_rows(project_id):
    qs = DeploymentJournal.objects.filter(num_of_images__gt=0).exclude(client_version__isnull=True).exclude(client_version='')
    if project_id:
        qs = qs.filter(project_id=project_id)
    rows = qs.values('client_version').annotate(uploads=Count('id'), n=Sum('num_of_images')).order_by('client_version')
    for x in rows:
        yield ['deployment_journal', x['client_version'], x['uploads'], x['n']]


def memo_rows(project_id):
    query = """SELECT split_part(memo, '/', 1) AS version, COUNT(*)
               FROM taicat_image
               WHERE memo LIKE 'APP-v%%' AND annotation_seq = 0"""
    params = []
    if project_id:
        query += ' AND project_id = %s'
        params.append(project_id)
    query += ' GROUP BY version ORDER BY version;'
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        for version, n in cursor.fetchall():
            # old memo has no per-upload record, so uploads is unknown
            yield ['image_memo', version, '', n]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default='client_version_stats.csv')
    parser.add_argument('--exclude-prerelease', action='store_true', help='skip alpha/beta/rc/dev/pre/snapshot versions')
    parser.add_argument('--project', type=int, help='limit to one project id (dashboard uses 329 for old versions)')
    args = parser.parse_args()

    with open(args.out, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['source', 'client_version', 'num_of_uploads', 'num_of_images'])
        for rows in (memo_rows(args.project), journal_rows(args.project)):
            for row in rows:
                if args.exclude_prerelease and is_prerelease(row[1]):
                    continue
                writer.writerow(row)
    print(f'saved {args.out}')


if __name__ == '__main__':
    main()
