# Usage Guide

A sample workflow:

1. Scaffold a new root (directory):
   ```sh
   rig init ~/dots
   ```
2. Add packages:
   ```sh
   rig add zsh git nvim
   ```
3. Browse available packages:
   ```sh
   rig search              # interactive browser
   rig search neovim       # pre-filtered search
   rig search --installed  # show only manifest packages
   ```
4. Add configs:
   ```sh
   mv ~/.config/zsh ~/dots/configs/zsh
   mv ~/.config/nvim ~/dots/configs/nvim
   ```
5. Sync everything:
   ```sh
   rig sync
   ```

see [index.md](index.md) for more details on the manifest and configuration.


