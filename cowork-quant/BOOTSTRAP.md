# One-time bootstrap (run on your machine — needs your GitHub auth)

The Cowork sandbox has no `gh` and no push credentials, and writing git
metadata through the mount corrupts it — so run these in a normal terminal
(PowerShell or WSL) from `E:\CS\github\hermes-quant`:

```bash
# 1. Create the private repo
gh repo create baladithyab/cowork-quant --private \
  --description "Claude Cowork plugin: PDR multi-analyst trading advisor (hermes-quant sibling)"

# 2. Init + first push (the files are already here)
cd cowork-quant
git init -b main
git add -A
git commit -m "feat: scaffold cowork-quant — plugin manifest, rails, inspiration-corpus research"
git remote add origin git@github.com:baladithyab/cowork-quant.git   # or https URL
git push -u origin main

# 3. Register as a submodule of hermes-quant
cd ..
git submodule add git@github.com:baladithyab/cowork-quant.git cowork-quant
git config -f .gitmodules submodule.cowork-quant.branch main
git add .gitmodules cowork-quant
git commit -m "feat(cowork): add cowork-quant submodule"
git push

# Day-to-day afterwards: commit+push inside cowork-quant/, then bump the
# pointer in the parent: git add cowork-quant && git commit -m "chore: bump submodule"
```

Note: `git submodule add` over an existing directory that is already a git
repo with the matching remote just registers it — your local history is kept.
Delete this file after bootstrap.
