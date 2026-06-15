# vendor/jeremy/v6.3.20/bin/ - binaries served from GitHub Releases

The 5 prebuilt linux-amd64 binaries that ship with this upstream are NOT in the git
tree (combined ~158 MB, kept out of git for repo health). They are mirrored to a
**GitHub Release** on this repo so operators have zero IPFS dependency.

## Get the binaries (no IPFS needed)

```bash
gh release download upstream-jeremy-v6.3.20-20260614 \
  -R Dagtechltd/dagtech-pool-stack \
  -p 'blockdag-node' -p 'mining-pool' -p 'nodeworker' \
  -p 'dashboard' -p 'dashboard-api' -p 'checksums.txt' \
  -D vendor/jeremy/v6.3.20/bin/
cd vendor/jeremy/v6.3.20/bin
sha256sum -c checksums.txt
chmod +x blockdag-node mining-pool nodeworker dashboard dashboard-api
```

Or direct curl (no `gh` CLI):
```bash
BASE=https://github.com/Dagtechltd/dagtech-pool-stack/releases/download/upstream-jeremy-v6.3.20-20260614
for f in blockdag-node mining-pool nodeworker dashboard dashboard-api checksums.txt; do
  curl -fL "$BASE/$f" -o "vendor/jeremy/v6.3.20/bin/$f"
done
cd vendor/jeremy/v6.3.20/bin && sha256sum -c checksums.txt
```

## Provenance

| Asset | Size | SHA256 |
|---|---|---|
| blockdag-node    | 95 MB  | ecfe5c3f494573057d1a387645966e0e1cdd7fadd83ac391dd2a00bd190783d7 |
| mining-pool      | 25 MB  | dfab5bf2d1a27409cabe9060d6e195dee645677a87fcf41b9ceaaa759afea494 |
| nodeworker       | 17 MB  | e6fbc789932e0acc67df03ac204c8519788b681a210929e6b08958e8c29a926f |
| dashboard        | 13 MB  | d232188ab3186c4d45529a4cefc81ed71c2609e872ef9607f835270a27ad2ea3 |
| dashboard-api    | 8.8 MB | a2bb1543a67ba94a976889a840a11ee43aa0509a90be4ed7f3db540d0de5b137 |

## IPFS provenance (forensic only, not the runtime fetch path)

- Upstream zip CID: `bafybeiga7llbde6jh2pzfdp2pvg3woqqxzy4aetmdijigaop67tu5ycffi`
- Upstream zip SHA256: `11363f5e91cea12a541ad39ee8df5fd02b7e479d60e7c1203b1d5c9803b4b553`
- Upstream IPNS (latest): `k51qzi5uqu5di0diurqi5rquevlgj4fv4ykm67tgovktd8vtnlimcvqywk1jhg`

The byte-identical zip is also attached to the same Release for full archival.
