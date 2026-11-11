"""OCR、时序候选及局部模板跟踪。启发式检测不等同于盲水印解码器。"""

import re
from collections import defaultdict, deque
from difflib import SequenceMatcher

import cv2
import numpy as np


def normalized(text):
    return re.sub(r"[\W_]", "", text).lower()


def identity_match(text, author):
    text, author = normalized(text), normalized(author)
    if any(token in text for token in ("抖音", "douyin", "tiktok")):
        return True
    return bool(
        len(author) >= 2
        and len(text) >= 2
        and (author in text or text in author or SequenceMatcher(None, text, author).ratio() >= 0.5)
    )


def clamp(box, width, height, pad=0):
    x1, y1, x2, y2 = box
    return (
        max(0, int(x1) - pad),
        max(0, int(y1) - pad),
        min(width, int(x2) + pad),
        min(height, int(y2) + pad),
    )


def highpass(gray):
    return cv2.absdiff(gray, cv2.GaussianBlur(gray, (0, 0), 2))


class Detector:
    def __init__(self, ocr, author="", reference=None):
        self.ocr = ocr
        self.author = author
        self.candidates = []
        self.history = deque(maxlen=5)
        self.last_scene = None
        self.last_sample = -10.0
        self.reference = reference or []

    def residual_match(self, item, identity_only=False):
        text = normalized(item["text"])
        if not text:
            return False
        for previous in self.reference:
            if identity_only and previous.get("kind") != "identity":
                continue
            if abs(previous["time"] - item["time"]) > 5:
                continue
            x1, y1, x2, y2 = previous["box"]
            extended = (x1 - 20, y1 - 20, x2 + 20, y2 + 20)
            if text in normalized(previous["text"]) and iou(extended, item["box"]) > 0.03:
                return True
        return False

    def needs_sample(self, frame, time):
        gray = cv2.cvtColor(cv2.resize(frame, (96, 54)), cv2.COLOR_BGR2GRAY)
        cut = self.last_scene is not None and np.mean(cv2.absdiff(gray, self.last_scene)) > 40
        self.last_scene = gray
        if time - self.last_sample >= 1.0 - 1e-6 or (cut and time - self.last_sample >= 0.25):
            self.last_sample = time
            return True
        return False

    def observe(self, frame, time):
        height, width = frame.shape[:2]
        # 保留边缘小字的原始分辨率，并提高灰色半透明签名的局部对比度。
        regions = [
            (0, 0, width, height),
            (0, int(height * 0.70), int(width * 0.4), height),
            (int(width * 0.6), int(height * 0.70), width, height),
            (0, 0, int(width * 0.4), int(height * 0.25)),
            (int(width * 0.6), 0, width, int(height * 0.25)),
        ]
        if self.reference:
            # 残留复检覆盖已发现目标附近区域；不把修复后新出现的场景文字当成新水印。
            regions = []
            nearby = sorted(self.reference, key=lambda item: abs(item["time"] - time))
            for previous in nearby:
                if abs(previous["time"] - time) > 5:
                    break
                region = clamp(previous["box"], width, height, 32)
                if not any(iou(region, old) > 0.4 for old in regions):
                    regions.append(region)
        found = []
        for index, (x1, y1, x2, y2) in enumerate(regions):
            crop = frame[y1:y2, x1:x2]
            if index or self.reference:
                gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
                gray = cv2.createCLAHE(clipLimit=3, tileGridSize=(8, 8)).apply(gray)
                crop = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
            output = self.ocr(crop)
            if output.boxes is None or output.txts is None:
                continue
            for polygon, text, score in zip(output.boxes, output.txts, output.scores):
                points = np.asarray(polygon)
                # 裁切区域中的半句字幕不能被当成外围签名。
                if index and (
                    (x1 > 0 and points[:, 0].min() < 6)
                    or (x2 < width and points[:, 0].max() > x2 - x1 - 6)
                ):
                    continue
                box = clamp(
                    (
                        points[:, 0].min() + x1,
                        points[:, 1].min() + y1,
                        points[:, 0].max() + x1,
                        points[:, 1].max() + y1,
                    ),
                    width,
                    height,
                    3,
                )
                if len(normalized(text)) < (1 if self.reference else 2) or float(score) < 0.35:
                    continue
                if any(iou(box, previous["box"]) > 0.5 for previous in found):
                    continue
                found.append(self._candidate(frame, time, box, text, float(score)))
        self.candidates.extend(found)
        # 图形候选需要跨多个变化画面保持边缘，且只在小面积外围区域成立。
        gray = cv2.cvtColor(cv2.resize(frame, (480, 270)), cv2.COLOR_BGR2GRAY)
        self.history.append(gray)
        if len(self.history) == 5:
            edges = [cv2.Canny(item, 35, 100) > 0 for item in self.history]
            stable = (np.sum(edges, axis=0) >= 4).astype(np.uint8) * 255
            stable = cv2.morphologyEx(stable, cv2.MORPH_CLOSE, np.ones((3, 5), np.uint8))
            contours, _ = cv2.findContours(stable, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for contour in contours:
                x, y, w, h = cv2.boundingRect(contour)
                if not (8 <= w <= 75 and 4 <= h <= 30 and (x < 90 or x > 370)):
                    continue
                margin = [
                    item[max(0, y - 8) : min(270, y + h + 8), max(0, x - 8) : min(480, x + w + 8)]
                    for item in self.history
                ]
                if np.std(np.stack(margin).astype(float), axis=0).mean() < 6:
                    continue
                box = clamp(
                    (
                        x * width / 480,
                        y * height / 270,
                        (x + w) * width / 480,
                        (y + h) * height / 270,
                    ),
                    width,
                    height,
                    3,
                )
                if not any(iou(box, item["box"]) > 0.2 for item in found):
                    self.candidates.append(self._candidate(frame, time, box, "", 0.5))

    @staticmethod
    def _candidate(frame, time, box, text, score):
        x1, y1, x2, y2 = box
        gray = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        return {"time": time, "box": box, "text": text, "score": score, "template": highpass(gray)}

    def select(self, width, height):
        groups = defaultdict(list)
        for item in self.candidates:
            key = normalized(item["text"])
            if key:
                groups[key].append(item)
        graphics = [item for item in self.candidates if not normalized(item["text"])]
        selected = []
        for item in self.candidates:
            x1, y1, x2, y2 = item["box"]
            key = normalized(item["text"])
            nearby = [
                other
                for other in groups[key]
                if abs(other["time"] - item["time"]) <= 5
                and abs(other["box"][0] - x1) < 32
                and abs(other["box"][1] - y1) < 24
            ]
            times = [other["time"] for other in nearby]
            # 边缘裁切后的半句字幕也有持续性，不能只靠水平位置排除。
            subtitle = y1 > height * 0.65 and (
                (x1 > width * 0.18 and x2 < width * 0.82) or y2 - y1 > height * 0.045
            )
            known = identity_match(item["text"], self.author) or self.residual_match(
                item, identity_only=subtitle
            )
            persistent = len(set(times)) >= 4 and max(times) - min(times) >= 4
            peripheral = x1 < width * 0.18 or x2 > width * 0.82 or y2 < height * 0.2
            small = (x2 - x1) * (y2 - y1) < width * height * 0.04
            stable_graphic = False
            if not key:
                nearby_graphics = [
                    other["time"]
                    for other in graphics
                    if abs(other["time"] - item["time"]) <= 12
                    and abs(other["box"][0] - x1) < 12
                    and abs(other["box"][1] - y1) < 12
                    and abs((other["box"][2] - other["box"][0]) - (x2 - x1)) < 12
                    and abs((other["box"][3] - other["box"][1]) - (y2 - y1)) < 12
                ]
                stable_graphic = (
                    len(set(nearby_graphics)) >= 8
                    and max(nearby_graphics) - min(nearby_graphics) >= 8
                )
            if known or (persistent and peripheral and small and not subtitle) or stable_graphic:
                track_key = key
                if known:
                    if any(token in key for token in ("抖音", "douyin", "tiktok")):
                        track_key = "platform"
                    elif identity_match(item["text"], self.author) or key in normalized(
                        self.author
                    ):
                        track_key = "author"
                selected.append(
                    {
                        **item,
                        "kind": "identity"
                        if known
                        else ("persistent_text" if key else "persistent_graphic"),
                        "subtitle_overlap": bool(subtitle and known),
                        "track_key": track_key,
                    }
                )
        return selected


def iou(a, b):
    area = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return area / max(1, (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - area)


class MaskTracker:
    def __init__(self, candidates, expanded=False):
        self.candidates = sorted(candidates, key=lambda item: item["time"])
        self.cursor = 0
        self.active = deque()
        self.expanded = expanded
        self.applied = []

    def repair(self, image, time):
        while (
            self.cursor < len(self.candidates) and self.candidates[self.cursor]["time"] <= time + 2
        ):
            self.active.append(self.candidates[self.cursor])
            self.cursor += 1
        while self.active and self.active[0]["time"] < time - 5:
            self.active.popleft()
        height, width = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        rectangles = []
        fallbacks = []
        subtitle_fallbacks = []
        result = image.copy()
        closest = {}
        for item in self.active:
            if item.get("kind") == "identity":
                key = item.get("track_key", "author")
                closest[key] = min(closest.get(key, float("inf")), abs(item["time"] - time))
        for item in sorted(self.active, key=lambda item: abs(item["time"] - time)):
            if item.get("kind") != "identity" and abs(item["time"] - time) > 0.65:
                continue
            if item.get("kind") == "identity" and abs(item["time"] - time) > (
                closest[item.get("track_key", "author")] + 0.25
            ):
                continue
            template = item["template"]
            x1, y1, x2, y2 = clamp(item["box"], width, height, 28)
            search = highpass(gray[y1:y2, x1:x2])
            if (
                min(template.shape) < 2
                or search.shape[0] < template.shape[0]
                or search.shape[1] < template.shape[1]
            ):
                continue
            scores = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
            _, score, _, position = cv2.minMaxLoc(scores)
            fallback = score < 0.42
            if fallback and item.get("kind") != "identity":
                continue
            if fallback:
                left, top = item["box"][:2]
            else:
                left, top = position[0] + x1, position[1] + y1
            box = (left, top, left + template.shape[1], top + template.shape[0])
            area = (box[2] - box[0]) * (box[3] - box[1])
            if any(
                iou(box, previous) > 0.35
                and area <= (previous[2] - previous[0]) * (previous[3] - previous[1]) * 1.15
                for previous in rectangles
            ):
                continue
            padding = max(5, int(template.shape[0] * 0.25)) if item.get("kind") == "identity" else 2
            x1, y1, x2, y2 = clamp(box, width, height, padding + (5 if self.expanded else 0))
            crop = image[y1:y2, x1:x2]
            response = highpass(gray[y1:y2, x1:x2])
            mask = (response > max(2, float(np.percentile(response, 55)))).astype(np.uint8) * 255
            mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)
            if self.expanded or fallback:
                mask[2:-2, 2:-2] = 255
            # 局部修复只改变 mask 像素；边界留出上下文供 Telea 插值。
            mask[:1] = mask[-1:] = 0
            mask[:, :1] = mask[:, -1:] = 0
            repaired = cv2.inpaint(crop, mask, 3, cv2.INPAINT_TELEA)
            result[y1:y2, x1:x2] = repaired
            rectangles.append((x1, y1, x2, y2))
            if fallback:
                fallbacks.append((x1, y1, x2, y2))
                if item.get("subtitle_overlap"):
                    subtitle_fallbacks.append((x1, y1, x2, y2))
        if rectangles:
            self.applied.append(
                {
                    "time": round(time, 6),
                    "boxes": rectangles,
                    "low_confidence_fallbacks": fallbacks,
                    "low_confidence_subtitle_regions": subtitle_fallbacks,
                }
            )
        return result


def describe(candidates):
    return [{key: value for key, value in item.items() if key != "template"} for item in candidates]
