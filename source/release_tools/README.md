# Release Tools

This folder contains the scripts used to package the bot for GitHub releases and generate SHA-256 checksums.

## Intended Use

These tools are intended for the project maintainer's private development environment.

The release script expects the following folder structure:

```text
blazing_bot_dev_updater/
├── bot_source/
│   └── dist/
│       └── NarutoBlazingBot/
└── release_tools/
    └── make_release.py
```

The public GitHub source repository does not include this complete development-kit structure. Therefore, the release script is not intended to be run directly from a fresh download of the public repository.

## Creating a Release

After successfully building the bot:

1. Open the `blazing_bot_dev_updater` folder.
2. Run the release script from the project root:

   `py release_tools/make_release.py 2.5.0`

3. The script generates:
   - `NarutoBlazingBot-win64.zip`
   - `SHA256SUMS.txt`

4. Upload both files to the corresponding GitHub Release.

Replace `2.5.0` with the version number being released.

---

Copyright © 2026 BlazingPytos. All rights reserved.
