"""Offline Russian Tesseract adapter for synthetic photo-card drafts."""
from __future__ import annotations

import os
import hashlib
import io
import warnings
import re
import shutil
import subprocess
from functools import lru_cache

from PIL import Image, ImageOps, UnidentifiedImageError

from .field_schema import FIELD_LABELS

TESSERACT_TIMEOUT_SECONDS = 15  # Temporary local component bound, not a production SLA.
MAX_OCR_TEXT_CHARS = 10_000
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 12_000_000
MAX_UNASSIGNED_LINES = 40
RECOGNITION_KEY = "_recognition"
FIELD_ALIASES = {
    "party": {"состав", "состав туристов", "состав путешественников"},
    "children_count": {"дети", "детей", "число детей", "количество детей", "дети в поездке"},
    "dates": {"даты", "дата", "даты поездки", "период поездки"},
    "budget": {"бюджет", "стоимость", "бюджет поездки"},
    "trip_type": {"тип поездки", "вид поездки", "тип отдыха"},
    "beach": {"пляж", "пляжные условия", "пляжный отдых"},
    "wishes": {"пожелания", "пожелание", "что важно"},
}
UNREADABLE_MARKERS = ("неразборчив", "нечитаем", "не читается")
UNKNOWN_MARKERS = ("не указано", "не указаны", "не указан", "нет данных", "не заполнено",
                   "неизвестно", "неизвестен", "неизвестна", "неизвестны", "нет сведений")
NEGATIVE_NUMBER_RE = re.compile(r"^[-−–—]\s*\d")
NO_CHILDREN_MARKERS = ("без детей", "нет детей", "детей нет", "0", "0 детей", "ноль детей")
EXCURSION_RE = re.compile(r"экскурс\w*|музе\w*|достопримечательност\w*|обзорн\w* тур", re.I)
EXCURSION_NEGATIVE_RE = re.compile(
    r"(?:без\s+экскурс\w*|экскурс\w*[^.;,]{0,24}\bне\s+(?:нуж\w*|интерес\w*|план\w*|хоч\w*)|"
    r"не\s+(?:нуж\w*|интерес\w*|хоч\w*)\s+экскурс\w*|не\s+экскурс\w*)", re.I
)
BEACH_RE = re.compile(r"пляж\w*|морск\w* отдых|на море|купани\w*|загорат\w*", re.I)
BEACH_NEGATIVE_RE = re.compile(
    r"(?:без пляжа|пляж\w*\s+(?:не нужен|не важ\w*|не обязател\w*|не интерес\w*)|не нужен пляж)", re.I
)
EXPLICIT_TRIP_TYPE_RE = re.compile(r"^(?:тип поездки|вид поездки|тип отдыха)\s*:", re.I)
CURRENCY_PATTERNS = (
    ("RUB", re.compile(r"(?:₽|руб(?:\.|лей|ля|ль)?|\bRUB\b)", re.I)),
    ("USD", re.compile(r"(?:\bUSD\b|\bUS\$|\$|доллар\w*)", re.I)),
    ("EUR", re.compile(r"(?:\bEUR\b|€|евро)", re.I)),
    ("TRY", re.compile(r"(?:\bTRY\b|турецк\w* лир\w*)", re.I)),
    ("KZT", re.compile(r"(?:\bKZT\b|тенге)", re.I)),
    ("BYN", re.compile(r"(?:\bBYN\b|белорус\w* рубл\w*)", re.I)),
    ("CNY", re.compile(r"(?:\bCNY\b|\bюан\w*|\byuan\b)", re.I)),
    ("GBP", re.compile(r"(?:\bGBP\b|£|\bфунт\w*|\bpound\w*)", re.I)),
    ("AED", re.compile(r"(?:\bAED\b|\bдирхам\w*|\bdirham\w*)", re.I)),
)
GENERIC_CURRENCY_CODE_RE = re.compile(r"(?<![A-Za-z])([A-Z]{3})(?![A-Za-z])", re.I)
UNLISTED_CURRENCY_WORD_RE = re.compile(r"(?:¥|\b(?:currency|yen|peso|franc|baht|bat|rupee|reais)\b|бат(?:а|ов|ы)?|валют\w*)", re.I)


def _empty_fields(kind: str, note: str) -> dict:
    fields = {
        key: {
            "value": "",
            "state": "unknown",
            "source": {"kind": kind, "page": 1,
                       "description": (note + (" Уточните сумму бюджета в рублях." if key == "budget" else ""))[:300]},
        }
        for key in FIELD_LABELS
    }
    fields["budget"]["currency"] = "RUB"
    return fields


@lru_cache(maxsize=8)
def _probe_engine(executable: str, tessdata_prefix: str | None) -> tuple[str, str] | None:
    env = os.environ.copy()
    if tessdata_prefix:
        env["TESSDATA_PREFIX"] = tessdata_prefix
    try:
        languages = subprocess.run(
            [executable, "--list-langs"], capture_output=True, timeout=5,
            check=False, env=env,
        )
        version = subprocess.run(
            [executable, "--version"], capture_output=True, timeout=5,
            check=False, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if languages.returncode or version.returncode:
        return None
    available = languages.stdout.decode("utf-8", "replace").splitlines()[1:]
    if "rus" not in {line.strip() for line in available}:
        return None
    first_line = version.stdout.decode("utf-8", "replace").splitlines()
    version = first_line[0].strip() if first_line else "version unknown"
    version = re.sub(r"^tesseract\s+", "", version, flags=re.IGNORECASE)
    return executable, version


def russian_tesseract_available() -> bool:
    executable = shutil.which("tesseract")
    return bool(executable and _probe_engine(executable, os.environ.get("TESSDATA_PREFIX")))


def _budget_value(candidate: str) -> tuple[str, str, str]:
    """Default only an absent currency to RUB; retain unknown currencies as conflicts."""
    currencies = [code for code, pattern in CURRENCY_PATTERNS if pattern.search(candidate)]
    unique = set(currencies)
    code_tokens = {token.upper() for token in GENERIC_CURRENCY_CODE_RE.findall(candidate)}
    unlisted_codes = code_tokens - unique
    unlisted_marker = bool(unlisted_codes or UNLISTED_CURRENCY_WORD_RE.search(candidate))
    if len(unique) == 1 and not unlisted_marker:
        currency = next(iter(unique))
    elif not unique and not unlisted_marker:
        currency = "RUB"
    elif not unique and len(unlisted_codes) == 1 and not UNLISTED_CURRENCY_WORD_RE.search(candidate):
        currency = next(iter(unlisted_codes))
    else:
        currency = "UNK"
    amount = candidate
    for _, pattern in CURRENCY_PATTERNS:
        amount = pattern.sub("", amount)
    amount = " ".join(amount.split()).strip(" \t:;,")
    state = "conflict" if (unlisted_marker or len(unique) > 1 or currency not in ("", "RUB")
                           or NEGATIVE_NUMBER_RE.match(amount)) else "ocr_candidate"
    return amount, currency, state


def _standalone_unknown(candidate: str) -> bool:
    folded = " ".join(candidate.casefold().split()).strip(" .,!;:")
    return (folded in UNKNOWN_MARKERS or bool(re.fullmatch(r"(?:не указ\w*|нет дан\w*|не заполн\w*)", folded))
            or bool(re.fullmatch(r"[?？]+|[-–—_]+", folded)))


def _standalone_unreadable(candidate: str) -> bool:
    folded = " ".join(candidate.casefold().split()).strip(" .,!;:")
    return folded in UNREADABLE_MARKERS or bool(
        re.fullmatch(r"(?:(?:запись|текст)\s+)?(?:неразборчив\w*|нечитаем\w*|не читается)", folded)
    )


def _standalone_unknown_or_unreadable(candidate: str) -> bool:
    return _standalone_unknown(candidate) or _standalone_unreadable(candidate)


def _children_value(candidate: str) -> str:
    folded = " ".join(candidate.casefold().split()).strip(" .,!;:")
    if folded in NO_CHILDREN_MARKERS:
        return "0"
    count = re.fullmatch(r"(\d+)\s*(?:дет(?:ей|и)|реб[её]н(?:ка|ок)?)?", folded)
    return count.group(1) if count else candidate


def _field_from_observations(key: str, observations: list[tuple[str, str]], engine_version: str) -> dict:
    prepared = []
    for candidate, line in observations:
        folded = candidate.casefold()
        currency = None
        unknown_value = not candidate or _standalone_unknown(candidate)
        unreadable_value = _standalone_unreadable(candidate)
        if unknown_value:
            value, state = "", "unknown"
            currency = "RUB" if key == "budget" else None
        elif unreadable_value:
            value, state = "", "unreadable"
            currency = "RUB" if key == "budget" else None
        elif key == "budget":
            value, currency, state = _budget_value(candidate)
        elif key == "children_count":
            value = _children_value(candidate)
            state = "conflict" if NEGATIVE_NUMBER_RE.match(value) else "ocr_candidate"
        else:
            value, state = candidate[:600], "ocr_candidate"
        prepared.append({"value": value, "state": state, "currency": currency, "line": line})

    signatures = {(item["state"], item["value"].casefold(), item.get("currency")) for item in prepared}
    differing = len(signatures) > 1
    repeated = len(prepared) > 1
    values = list(dict.fromkeys(item["value"] for item in prepared if item["value"]))
    currencies = {item.get("currency") for item in prepared if item.get("currency")}
    if differing:
        state = "conflict"
        value = " / ".join(values)[:600]
        currency = currencies.pop() if len(currencies) == 1 else ("UNK" if currencies else "RUB")
        note = "Повторные строки OCR расходятся; сохранены варианты: "
    else:
        state = prepared[0]["state"]
        value = prepared[0]["value"]
        currency = prepared[0].get("currency")
        note = (f"Получено {len(prepared)} одинаковых строк OCR; "
                "значение и все источники сохранены: ") if repeated else ""
    source_lines = [item["line"] if item["value"] or item["state"] != "unknown"
                    else item["line"] + " (пустое значение)" for item in prepared]
    description = f"{engine_version}; {note}{' | '.join(source_lines)}"[:300]
    field = {"value": value, "state": state,
             "source": {"kind": "local_tesseract", "page": 1, "description": description}}
    if key == "budget":
        field["currency"] = currency or "RUB"
    return field


def _trip_type_from_text(text: str, explicit_lines: list[str]) -> tuple[str, str, str]:
    """Infer only a manager-review candidate from whole-card cues; retain contradictions."""
    lines = [" ".join(line.split()) for line in text[:MAX_OCR_TEXT_CHARS].splitlines() if line.strip()]
    contents = []
    for line in lines:
        label, separator, value = line.partition(":")
        content = value.strip() if separator else line
        if content:
            contents.append((line, label.casefold().strip(), content))
    other_lines = [(line, label, content) for line, label, content in contents
                   if not EXPLICIT_TRIP_TYPE_RE.match(line)]
    excursion_positive = []
    excursion_negative = []
    beach_positive = []
    beach_negative = []
    for line, label, content in contents:
        if EXCURSION_RE.search(content):
            target = excursion_negative if EXCURSION_NEGATIVE_RE.search(content) else excursion_positive
            target.append(line)
        explicit_beach_field = label in FIELD_ALIASES['beach']
        if not _standalone_unknown_or_unreadable(content) and (BEACH_RE.search(content) or explicit_beach_field):
            if BEACH_NEGATIVE_RE.search(content):
                beach_negative.append(line)
            else:
                beach_positive.append(line)

    explicit_types: set[str] = set()
    for line in explicit_lines:
        candidate = line.split(":", 1)[-1].casefold()
        excursion = bool(EXCURSION_RE.search(candidate) and not EXCURSION_NEGATIVE_RE.search(candidate))
        beach = bool(BEACH_RE.search(candidate) and not BEACH_NEGATIVE_RE.search(candidate))
        if re.search(r"смешанн\w*|комбинированн\w*", candidate):
            excursion = beach = True
        if excursion:
            explicit_types.add("excursion")
        if beach:
            explicit_types.add("beach")

    explicit_excursion = "excursion" in explicit_types
    explicit_beach = "beach" in explicit_types
    other_excursion = any(EXCURSION_RE.search(content) and not EXCURSION_NEGATIVE_RE.search(content)
                          for _, _, content in other_lines)
    other_beach = any(line in beach_positive for line, _, _ in other_lines)
    if (explicit_excursion and other_beach) or (explicit_beach and other_excursion):
        evidence = explicit_lines + excursion_positive + excursion_negative + beach_positive + beach_negative
        return ("Экскурсионный / пляжный — требуется уточнение", "conflict",
                "Признаки типа расходятся по строкам: " + " | ".join(dict.fromkeys(evidence))[:240])
    if beach_negative and beach_positive:
        evidence = explicit_lines + excursion_positive + excursion_negative + beach_positive + beach_negative
        return ("Противоречивые признаки пляжа — требуется уточнение", "conflict",
                "Признаки типа расходятся по строкам: " + " | ".join(dict.fromkeys(evidence))[:240])
    if excursion_positive and excursion_negative:
        evidence = explicit_lines + excursion_positive + excursion_negative + beach_positive + beach_negative
        return ("Противоречивые признаки типа поездки — требуется уточнение", "conflict",
                "Признаки типа расходятся по строкам: " + " | ".join(dict.fromkeys(evidence))[:240])

    excursion = explicit_excursion or bool(excursion_positive)
    beach = explicit_beach or bool(beach_positive)
    evidence = list(dict.fromkeys(explicit_lines + excursion_positive + excursion_negative + beach_positive))
    if excursion and beach:
        return "Смешанный: экскурсии и пляж", "ocr_candidate", "Тип-кандидат по всему листу: " + " | ".join(evidence)[:240]
    if excursion:
        return "Экскурсионный", "ocr_candidate", "Тип-кандидат по всему листу: " + " | ".join(evidence)[:240]
    if beach:
        return "Пляжный отдых", "ocr_candidate", "Тип-кандидат по всему листу: " + " | ".join(evidence)[:240]
    return "", "unknown", "Явных признаков типа поездки недостаточно; не угадывать по одному пустому полю."


def parse_card_text(text: str, *, engine_version: str = "Tesseract") -> tuple[dict, dict]:
    """Map repeated labels and whole-card context without silently choosing OCR output."""
    normalized_aliases = {
        key: {re.sub(r"\s+", " ", alias.casefold()) for alias in aliases}
        for key, aliases in FIELD_ALIASES.items()
    }
    matched: dict[str, list[tuple[str, str]]] = {}
    unassigned_lines = []
    unassigned_count = 0
    for line_number, raw_line in enumerate(text[:MAX_OCR_TEXT_CHARS].splitlines(), 1):
        line = " ".join(raw_line.split()).strip(" \t\r\n•*-—")
        if not line:
            continue
        label, separator, candidate = line.partition(":")
        label = re.sub(r"\s+", " ", label.casefold().strip())
        candidate = candidate.strip(" \t\r\n•*")
        for key, aliases in normalized_aliases.items():
            if separator and label in aliases:
                matched.setdefault(key, []).append((candidate, line[:300]))
                break
        else:
            unassigned_count += 1
            if len(unassigned_lines) < MAX_UNASSIGNED_LINES:
                unassigned_lines.append({"line": line_number, "text": line[:300]})

    fields = {}
    for key in FIELD_LABELS:
        observations = matched.get(key)
        if observations:
            fields[key] = _field_from_observations(key, observations, engine_version)
        else:
            fields[key] = {"value": "", "state": "unknown",
                           "source": {"kind": "local_tesseract", "page": 1,
                                      "description": f"OCR не нашёл явную строку поля «{FIELD_LABELS[key]}». Сверьте по фото."}}

    explicit_type_lines = [line for _, line in matched.get("trip_type", [])]
    trip_value, trip_state, trip_note = _trip_type_from_text(text, explicit_type_lines)
    if (matched.get("trip_type") and fields["trip_type"]["state"] in ("unknown", "unreadable")
            and any(candidate for candidate, _ in matched["trip_type"])):
        # An explicit absence/unreadability marker is not overridden by inferred cues.
        # Other fields and the full original text remain available for manual review.
        trip_value = ""
        trip_state = fields["trip_type"]["state"]
        trip_note = "Явная отметка неизвестности/нечитаемости сохранена; тип не выведен из других полей. " + fields["trip_type"]["source"]["description"]
    elif matched.get("trip_type") and fields["trip_type"]["state"] == "conflict":
        trip_value = fields["trip_type"]["value"]
        trip_state = "conflict"
        trip_note = fields["trip_type"]["source"]["description"]
    elif matched.get("trip_type") and trip_state == "unknown":
        explicit_value = " / ".join(candidate for candidate, _ in matched["trip_type"] if candidate)
        if explicit_value:
            trip_value = explicit_value[:600]
            trip_state = "conflict"
            trip_note = "Тип поездки указан неоднозначно; требуется подтверждение менеджера: " + " | ".join(
                line for _, line in matched["trip_type"]
            )[:240]
    fields["trip_type"] = {"value": trip_value, "state": trip_state,
                            "source": {"kind": "local_tesseract", "page": 1,
                                       "description": f"{engine_version}; {trip_note}"[:300]}}

    if not matched.get("beach") or (
        fields["beach"]["state"] == "unknown"
        and not any(_standalone_unknown_or_unreadable(candidate) for candidate, _ in matched.get("beach", []))
        and trip_value == "Экскурсионный" and trip_state == "ocr_candidate"
    ):
        if trip_value == "Экскурсионный" and trip_state == "ocr_candidate":
            fields["beach"] = {"value": "", "state": "not_applicable",
                               "source": {"kind": "local_tesseract", "page": 1,
                                          "description": "Экскурсионная поездка: пляж не является обязательным полем."}}
        else:
            beach_note = ("Смешанный запрос: уточнить пляжные условия, если они важны."
                          if trip_value.startswith("Смешанный") else
                          "Тип поездки неоднозначен или неизвестен; пустое поле пляжа нельзя объявить неприменимым.")
            fields["beach"] = {"value": "", "state": "unknown",
                               "source": {"kind": "local_tesseract", "page": 1, "description": beach_note}}
    elif fields["beach"]["value"] and BEACH_NEGATIVE_RE.search(fields["beach"]["value"]):
        if trip_value == "Экскурсионный":
            fields["beach"].update(value="", state="not_applicable")

    if not matched.get("children_count"):
        fields["children_count"] = {"value": "", "state": "unknown",
                                    "source": {"kind": "local_tesseract", "page": 1,
                                               "description": "Число детей не указано; не выводить ноль из состава взрослых."}}

    if not matched.get("budget"):
        fields["budget"]["currency"] = "RUB"
        fields["budget"]["source"]["description"] = "Бюджет не найден; уточнить сумму у менеджера в рублях."
    elif fields["budget"]["state"] in ("unknown", "unreadable"):
        fields["budget"]["currency"] = "RUB"
        fields["budget"]["source"]["description"] += " Уточнить сумму в рублях."
    elif fields["budget"]["state"] == "conflict" and fields["budget"].get("currency") not in ("", "RUB"):
        fields["budget"]["source"]["description"] += " Валюта конфликтует с рублёвым сценарием; не конвертировать."

    clarifications = []
    budget = fields["budget"]
    if budget["state"] in ("unknown", "unreadable"):
        clarifications.append("Уточнить сумму бюджета у менеджера в рублях; валюту повторно не спрашивать.")
    elif budget["state"] == "conflict":
        if budget.get("currency") not in ("", "RUB"):
            clarifications.append(f"В исходнике валюта {budget['currency']}; уточнить сумму в рублях, не конвертировать автоматически.")
        elif NEGATIVE_NUMBER_RE.match(budget["value"]):
            clarifications.append("Отрицательный бюджет: знак сохранён. Сверьте сумму в рублях по оригиналу; знак не исправляется автоматически.")
        else:
            clarifications.append("Повторные строки бюджета расходятся; сверить по фото и уточнить сумму в рублях.")
    if fields["children_count"]["state"] == "conflict" and NEGATIVE_NUMBER_RE.match(fields["children_count"]["value"]):
        clarifications.append("Отрицательное число детей: знак сохранён. Сверьте значение по оригиналу.")

    observations = {key: [line for _, line in entries] for key, entries in matched.items()}
    has_extraction = bool(matched) or trip_state in ("ocr_candidate", "conflict")
    note = ("OCR дал черновые кандидаты; менеджер проверяет их по всему листу. Неизвестные поля не становятся нулём."
            if has_extraction else
            "OCR не нашёл пригодных полей. Сверьте фото и заполните неизвестные значения вручную.")
    metadata = {"engine": "Tesseract", "version": engine_version, "language": "rus",
                "status": "recognized" if has_extraction else "no_fields_found", "note": note,
                "field_observations": observations, "clarifications": clarifications,
                "unassigned_lines": unassigned_lines, "unassigned_count": unassigned_count,
                "raw_text": text[:MAX_OCR_TEXT_CHARS]}
    return fields, metadata


def _prepare_ocr_image(image_bytes: bytes) -> tuple[bytes, dict]:
    """Normalize orientation in memory; never modify or export the private original."""
    if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("image size")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(image_bytes)) as source:
            if (source.format not in ("PNG", "JPEG") or source.width * source.height > MAX_IMAGE_PIXELS
                    or getattr(source, "n_frames", 1) != 1):
                raise ValueError("image format or dimensions")
            source.verify()
        with Image.open(io.BytesIO(image_bytes)) as source:
            orientation = source.getexif().get(274, 1)
            if type(orientation) is not int or orientation not in range(1, 9):
                raise ValueError("invalid orientation")
            source.load()
            oriented = ImageOps.exif_transpose(source)
            # Fresh pixels omit EXIF/comments and use white for transparent areas.
            normalized = Image.new("RGB", oriented.size, "white")
            rgba = oriented.convert("RGBA")
            normalized.paste(rgba, mask=rgba.getchannel("A"))
            output = io.BytesIO()
            normalized.save(output, format="PNG")
    prepared = output.getvalue()
    return prepared, {
        "operation": "exif_transpose", "orientation": orientation,
        "applied": orientation != 1, "width": normalized.width, "height": normalized.height,
        "source_sha256": hashlib.sha256(image_bytes).hexdigest(),
        "ocr_input_sha256": hashlib.sha256(prepared).hexdigest(),
    }


def recognize_card_photo(image_bytes: bytes) -> tuple[dict, dict]:
    """Run local-only OCR. Missing engine/model or failures preserve a manual path."""
    executable = shutil.which("tesseract")
    engine = _probe_engine(executable, os.environ.get("TESSDATA_PREFIX")) if executable else None
    if not engine:
        note = "Локальный OCR не выполнен: установите Tesseract с русской моделью; заполните или отметьте поля вручную."
        metadata = {"engine": "Tesseract", "version": "", "language": "rus",
                    "status": "unavailable", "note": note}
        return _empty_fields("manual_fallback", note), metadata

    executable, version = engine
    try:
        prepared_image, preprocessing = _prepare_ocr_image(image_bytes)
    except (ValueError, OSError, SyntaxError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        note = "Фото не удалось безопасно подготовить для OCR. Сверьте оригинал вручную или загрузите исправный PNG/JPEG."
        return _empty_fields("manual_fallback", note), {
            "engine": "Tesseract", "version": version, "language": "rus", "status": "error", "note": note,
        }
    try:
        result = subprocess.run(
            [executable, "stdin", "stdout", "--psm", "6", "-l", "rus"],
            input=prepared_image, capture_output=True, timeout=TESSERACT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        note = "Локальный OCR превысил тестовый лимит времени. Проверьте поля вручную."
        metadata = {"engine": "Tesseract", "version": version, "language": "rus",
                    "status": "timeout", "note": note}
        return _empty_fields("manual_fallback", note), metadata
    except OSError:
        note = "Локальный OCR не удалось запустить. Проверьте поля вручную."
        metadata = {"engine": "Tesseract", "version": version, "language": "rus",
                    "status": "error", "note": note}
        return _empty_fields("manual_fallback", note), metadata

    if result.returncode:
        note = "Локальный OCR завершился ошибкой. Исходник сохранён; проверьте поля вручную."
        metadata = {"engine": "Tesseract", "version": version, "language": "rus",
                    "status": "error", "note": note}
        return _empty_fields("manual_fallback", note), metadata

    text = result.stdout.decode("utf-8", "replace")[:MAX_OCR_TEXT_CHARS]
    if not text.strip():
        note = "OCR не обнаружил текст. Это не доказывает, что поля пусты; проверьте фото вручную."
        metadata = {"engine": "Tesseract", "version": version, "language": "rus",
                    "status": "no_text", "note": note}
        return _empty_fields("manual_fallback", note), metadata
    fields, metadata = parse_card_text(text, engine_version=f"{version}, rus")
    metadata["preprocessing"] = preprocessing
    return fields, metadata
