# Upstream attribution

Visual Media is a modified, integrated distribution of the following source snapshots taken from the user's existing AstrBot installation on 2026-11-11:

- **表情包小偷 / astrbot_plugin_stealer**, version `3.1.13+local.2`, author **nagatoquin33**. Repository: https://github.com/nagatoquin33/astrbot_plugin_stealer . Source license: GNU Affero General Public License, version 3.
- **Gemini 图像生成 / astrbot_plugin_gemini_image_generation**, version `v4.0.0`, author **piexian**. Repository: https://github.com/piexian/astrbot_plugin_gemini_image_generation . Source license: GNU Affero General Public License, version 3.

Both original license texts are retained under `licenses/`; the combined project includes the AGPL-3.0 license in `LICENSE`. Upstream documentation and inline notices remain in the source tree. Bundled third-party frontend assets retain their embedded notices and dependencies are recorded in the frontend lockfile.

Integration changes include a single host entry point, internal engine ownership and shutdown, grouped configuration, namespaced routes and data, a unified management view, character profiles, generation-to-meme persistence, conversation-scoped access, generation-turn coordination, copy-only migration, and integration tests. The installed source snapshots include local changes and are not claimed to be pristine upstream release tags.
