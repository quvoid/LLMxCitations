import csv, sys, re
from urllib.parse import urlparse
import pandas as pd

csv.field_size_limit(sys.maxsize)

input_csv = "glass_output.csv"
output_xlsx = "glass_citations_report.xlsx"

glass_keywords = {
    'Saint-Gobain': ['saint-gobain', 'saint gobain', 'sun ban'],
    'AIS (Asahi India)': ['ais', 'asahi', 'asahi india', 'ais glass'],
    'Guardian Glass / Modiguard': ['guardian', 'modiguard', 'gujarat guardian'],
    'Goldplus Glass': ['goldplus', 'gold plus'],
    'Sisecam': ['sisecam', 'şişecam'],
    'Pilkington': ['pilkington', 'nsg'],
    'FG Glass': ['fg glass'],
    'GSC Glass': ['gsc glass', 'gsc'],
    'Sezal Glass': ['sezal'],
}

rows_clean = []
with open(input_csv, 'r', encoding='utf-8-sig') as f:
    reader = csv.DictReader(f)
    for r in reader:
        url = r.get('url', '').strip()
        domain = urlparse(url).netloc.lower().replace('www.', '') if url else ''
        
        resp_text = r.get('response_content', '')
        # Truncate response text so it never exceeds Excel's 32,767 char limit
        safe_response = resp_text[:1000] + ('...' if len(resp_text) > 1000 else '')
        
        # Detect mentioned brands in the response
        detected_brands = []
        full_text = (resp_text + ' ' + url).lower()
        for b, aliases in glass_keywords.items():
            if any(a in full_text for a in aliases):
                detected_brands.append(b)
        
        rows_clean.append({
            'Prompt': r.get('prompt', ''),
            'Platform': r.get('platform', '').upper(),
            'Citation URL': url,
            'Domain': domain,
            'Citation Category': r.get('citation_category', ''),
            'Mentioned Brands': ', '.join(detected_brands) if detected_brands else 'None',
            'Response Date': r.get('response_date', ''),
            'Response Preview': safe_response
        })

df_all = pd.DataFrame(rows_clean)

# Create Brand SOV Summary
brand_counts = {b: 0 for b in glass_keywords}
domain_counts = {}

for r in rows_clean:
    brands_str = r['Mentioned Brands']
    if brands_str != 'None':
        for b in brands_str.split(', '):
            if b in brand_counts:
                brand_counts[b] += 1
    d = r['Domain']
    if d:
        domain_counts[d] = domain_counts.get(d, 0) + 1

total_mentions = sum(brand_counts.values())
sov_rows = []
for b, count in sorted(brand_counts.items(), key=lambda x: x[1], reverse=True):
    sov_pct = (count / total_mentions * 100) if total_mentions > 0 else 0
    sov_rows.append({
        'Brand Name': b,
        'Mentions Count': count,
        'Share of Voice (SOV %)': f"{sov_pct:.2f}%"
    })
df_sov = pd.DataFrame(sov_rows)

# Create Domain Rankings
domain_rows = []
for d, count in sorted(domain_counts.items(), key=lambda x: x[1], reverse=True):
    domain_rows.append({
        'Domain': d,
        'Citations Count': count
    })
df_domains = pd.DataFrame(domain_rows)

# Write to multi-sheet formatted Excel workbook
with pd.ExcelWriter(output_xlsx, engine='openpyxl') as writer:
    df_all.to_excel(writer, sheet_name='All Citations', index=False)
    df_sov.to_excel(writer, sheet_name='Brand Share of Voice', index=False)
    df_domains.to_excel(writer, sheet_name='Top Cited Domains', index=False)

print(f"Successfully generated clean Excel file: {output_xlsx}")
