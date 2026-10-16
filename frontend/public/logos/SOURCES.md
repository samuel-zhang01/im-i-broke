# Institution logos

The initial set was retrieved 2026-09-19 from official websites and publisher App Store listings. Stored locally as PNGs; no third-party requests occur when viewing accounts. Logos remain the property of their respective owners. ICO/app artwork is decoded to PNG, preserving its aspect ratio.

- **hsbc** (180 × 180): [source](https://www.hsbc.co.uk/etc.clientlibs/dpws/clientlibs-public/clientlib-site/resources/favicons/apple-touch-icon.png)
- **lloyds** (256 × 256): [source](https://www.lloydsbank.com/content/dam/lloyds/personal/lloyds-personal-app-logo1.png)
- **revolut** (256 × 256): [source](https://is1-ssl.mzstatic.com/image/thumb/Purple211/v4/1f/7c/14/1f7c1428-a432-8d16-9b55-0cb08bd77575/AppIcon-Production-0-0-1x_U007epad-0-1-85-220.png/512x512bb.jpg)
- **american-express** (256 × 256): [source](https://is1-ssl.mzstatic.com/image/thumb/Purple211/v4/fb/ca/14/fbca14f8-b632-998e-bfb2-af319e1988ee/AppIcon-0-0-1x_U007epad-0-1-0-85-220.png/512x512bb.jpg)
- **starling** (231 × 231): [source](https://www.starlingbank.com/media-library/sb-logo.png)
- **barclays** (180 × 180): [source](https://www.barclays.co.uk/content/dam/icons/favicons/barclays/apple-touch-icon-180x180.png)
- **trading212** (256 × 256): [source](https://is1-ssl.mzstatic.com/image/thumb/Purple211/v4/05/fd/82/05fd829d-7087-1a55-90ab-ab8e4ccc8081/AppIcon-0-0-1x_U007epad-0-1-85-220.png/512x512bb.jpg)
- **moomoo** (256 × 256): [source](https://is1-ssl.mzstatic.com/image/thumb/Purple221/v4/08/7a/76/087a76d8-161f-ff31-fd81-397b0be78959/AppIcon-MooMoo-0-0-1x_U007epad-0-1-sRGB-85-220.png/512x512bb.jpg)

## Quality replacements (2026-09-28)

- **dbs** (256 × 256): The red mark was cropped from the [Wikipedia-hosted DBS vector](https://en.wikipedia.org/wiki/File:DBS_Bank_Logo_(alternative).svg) and rendered as PNG.
- **lunchflow** (256 × 256): The mark was rendered from the inline vector logo on the [official Lunch Flow homepage](https://www.lunchflow.app/).
- **monzo** (256 × 256): The publisher's App Store icon replaced the 96 px favicon. Its exact artwork URL is in [`institutionCatalog.json`](../../src/institutionCatalog.json).
- **snaptrade** (245 × 256): The full-colour icon comes from the [official SnapTrade brand kit](https://snaptrade.com/brand).
- **fidelity, robinhood, schwab, stake-aus, tradier, upstox, vanguard** (256 × 256): Sharper artwork from each publisher's App Store listing replaced visibly pixelated catalog assets. Exact artwork URLs and retrieval dates are in [`institutionCatalog.json`](../../src/institutionCatalog.json). These entries have `curated_artwork` so a directory refresh retains the reviewed sources.

## Published SnapTrade directory

On 2026-09-23, the catalog was expanded to all 43 integrations in
[SnapTrade's public brokerage directory](https://snaptrade.com/brokerage-integrations).
The exact institution page, artwork URL and retrieval date for each asset are in
[`institutionCatalog.json`](../../src/institutionCatalog.json). Trading 212 and
Moomoo now use that catalog's artwork instead of the older App Store images above.
DEGIRO uses its official website's Apple touch icon because its catalog artwork
was absent. The remaining existing bank/provider images are retained.

Run `python3 scripts/sync_institution_logos.py --check` from the repository root
to verify bundled assets offline. Maintainers can update the directory with
`--refresh` using Pillow and CairoSVG. Review the diff before release. Removed
integrations retain their artwork for saved accounts but leave the advertised
provider list. Unknown institutions display initials and still support accounts.
