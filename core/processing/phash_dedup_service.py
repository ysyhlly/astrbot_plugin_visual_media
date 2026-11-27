"""感知哈希去重服务：负责计算 pHash 和汉明距离比较。"""

import asyncio
from typing import Any

from astrbot.api import logger

try:
    from PIL import Image as PILImage
except Exception:
    PILImage = None


class PHashDedupService:
    """基于感知哈希 (pHash) 的图片视觉去重服务。"""

    PHASH_SIZE = 16  # 感知哈希图片缩放尺寸（16x16 = 256 bit hash）
    PHASH_HAMMING_THRESHOLD = 20  # 汉明距离阈值，低于此值视为重复

    def __init__(self, plugin_instance: Any = None) -> None:
        self.plugin = plugin_instance

    async def compute_phash(self, file_path: str) -> str:
        """计算图片的感知哈希 (pHash)，用于视觉去重。

        使用 difference hash (dHash) 算法：
        - 将图片缩放到 (PHASH_SIZE+1) x PHASH_SIZE 的灰度图
        - 比较相邻像素的亮度差异生成哈希位
        - 动图仅使用 SHA256 去重，避免首帧相同的不同动画被丢弃

        Args:
            file_path: 图片文件路径

        Returns:
            str: 十六进制感知哈希字符串，失败返回空字符串
        """
        if PILImage is None:
            return ""

        size = self.PHASH_SIZE

        def _sync_phash(fp: str) -> str:
            try:
                with PILImage.open(fp) as im:
                    if getattr(im, "is_animated", False):
                        return ""
                    # 转灰度并缩放到 (size+1) x size
                    gray = im.convert("L").resize((size + 1, size), PILImage.BILINEAR)
                    pixels = list(gray.getdata())

                    # dHash: 比较每行相邻像素
                    bits = []
                    for row in range(size):
                        for col in range(size):
                            idx = row * (size + 1) + col
                            bits.append(1 if pixels[idx] > pixels[idx + 1] else 0)

                    # 转换为十六进制字符串
                    hash_int = 0
                    for bit in bits:
                        hash_int = (hash_int << 1) | bit
                    hex_len = (size * size + 3) // 4  # 向上取整
                    return format(hash_int, f"0{hex_len}x")
            except Exception as e:
                logger.warning(f"计算感知哈希失败: {e}")
                return ""

        try:
            return await asyncio.to_thread(_sync_phash, file_path)
        except Exception as e:
            logger.warning(f"计算感知哈希失败: {e}")
            return ""

    @staticmethod
    def hamming_distance(hash1: str, hash2: str) -> int:
        """计算两个十六进制哈希字符串的汉明距离。

        Args:
            hash1: 第一个十六进制哈希
            hash2: 第二个十六进制哈希

        Returns:
            int: 汉明距离（不同位的数量），如果无法计算返回极大值
        """
        if not hash1 or not hash2 or len(hash1) != len(hash2):
            return 999
        try:
            xor = int(hash1, 16) ^ int(hash2, 16)
            return xor.bit_count()
        except (ValueError, TypeError):
            return 999

    @classmethod
    def cluster_similar_images(
        cls,
        entries: list[dict[str, Any]],
        *,
        threshold: int | None = None,
    ) -> list[dict[str, Any]]:
        """Cluster visually similar entries by pHash Hamming distance using Union-Find.

        Args:
            entries: List of meme metadata dicts containing at least ``hash`` and ``phash``.
            threshold: Max Hamming distance to consider two images similar.

        Returns:
            List of cluster dicts (size >= 2), sorted by cluster size descending.
        """
        limit = (
            cls.PHASH_HAMMING_THRESHOLD
            if threshold is None
            else max(0, min(128, int(threshold)))
        )
        valid: list[dict[str, Any]] = []
        valid_ints: list[tuple[int, int]] = []
        seen_hashes: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            img_hash = str(entry.get("hash") or "").strip()
            phash = str(entry.get("phash") or "").strip()
            if not img_hash or not phash or img_hash in seen_hashes:
                continue
            try:
                ph_int = int(phash, 16)
            except (ValueError, TypeError):
                continue
            seen_hashes.add(img_hash)
            valid.append(entry)
            valid_ints.append((len(phash), ph_int))

        n = len(valid)
        if n < 2:
            return []

        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for i in range(n):
            len_i, int_i = valid_ints[i]
            for j in range(i + 1, n):
                len_j, int_j = valid_ints[j]
                if len_i != len_j:
                    continue
                if (int_i ^ int_j).bit_count() <= limit:
                    union(i, j)

        groups: dict[int, list[int]] = {}
        for idx in range(n):
            root = find(idx)
            groups.setdefault(root, []).append(idx)

        def _keeper_rank(item: dict[str, Any]) -> tuple[int, int, int, int]:
            fav = 1 if item.get("is_favorite") else 0
            try:
                uses = int(item.get("use_count") or 0)
            except (TypeError, ValueError):
                uses = 0
            try:
                area = int(item.get("width") or 0) * int(item.get("height") or 0)
            except (TypeError, ValueError):
                area = 0
            try:
                created = int(item.get("created_at") or 0)
            except (TypeError, ValueError):
                created = 0
            return (fav, uses, area, -created)

        clusters: list[dict[str, Any]] = []
        for member_indices in groups.values():
            if len(member_indices) < 2:
                continue
            member_indices.sort(key=lambda idx: _keeper_rank(valid[idx]), reverse=True)
            keeper_idx = member_indices[0]
            keeper = valid[keeper_idx]
            keeper_ph = str(keeper.get("phash") or "")
            items_out: list[dict[str, Any]] = []
            max_dist = 0
            for idx in member_indices:
                item = valid[idx]
                dist = (
                    0
                    if idx == keeper_idx
                    else cls.hamming_distance(keeper_ph, str(item.get("phash") or ""))
                )
                if dist < 999:
                    max_dist = max(max_dist, dist)
                items_out.append(
                    {
                        "hash": str(item.get("hash") or ""),
                        "category": str(item.get("category") or ""),
                        "desc": str(item.get("desc") or ""),
                        "overlay_text": str(item.get("overlay_text") or ""),
                        "character": str(item.get("character") or ""),
                        "use_count": int(item.get("use_count") or 0),
                        "is_favorite": bool(item.get("is_favorite")),
                        "width": int(item.get("width") or 0),
                        "height": int(item.get("height") or 0),
                        "bytes": int(item.get("bytes") or 0),
                        "scope_mode": str(item.get("scope_mode") or "public"),
                        "distance": dist if dist < 999 else 0,
                        "recommended": idx == keeper_idx,
                    }
                )
            clusters.append(
                {
                    "cluster_id": str(keeper.get("hash") or "")[:16],
                    "size": len(items_out),
                    "recommended_keep": str(keeper.get("hash") or ""),
                    "max_distance": max_dist,
                    "items": items_out,
                }
            )

        clusters.sort(key=lambda c: (-c["size"], c["max_distance"], c["cluster_id"]))
        return clusters
