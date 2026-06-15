# vendor/jeremy/v6.3.20/bin/ — IPFS-pinned binaries

These five binaries are **not committed to git** (combined 157 MB, exceeds reasonable
git-repo health). They are byte-identical to what Jeremy ships in his IPFS release at
CID `bafybeiga7llbde6jh2pzfdp2pvg3woqqxzy4aetmdijigaop67tu5ycffi`
(zip SHA256 `11363f5e91cea12a541ad39ee8df5fd02b7e479d60e7c1203b1d5c9803b4b553`).

Fetch + verify locally:
```bash
ZIP=pool-stack-docker-v6.3.20-jeremy-dev-release.20260614-linux-amd64.zip
curl -fL https://dweb.link/ipfs/bafybeiga7llbde6jh2pzfdp2pvg3woqqxzy4aetmdijigaop67tu5ycffi -o $ZIP
echo "11363f5e91cea12a541ad39ee8df5fd02b7e479d60e7c1203b1d5c9803b4b553  $ZIP" | sha256sum -c -
unzip -j $ZIP "*/bin/*" -d bin/
```

| File | Size | Role |
|---|---|---|
| blockdag-node    | 95 MB | QNG core node |
| mining-pool      | 25 MB | Jeremy's mining pool (replacement for asic-pool) |
| nodeworker       | 17 MB | Child node manager |
| dashboard        | 13 MB | UI tier |
| dashboard-api    | 8.8 MB | API tier behind dashboard |
