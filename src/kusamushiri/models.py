import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal

from kusamushiri.i18n import tr

MediaFilter = Literal["all", "with_media", "without_media"]
SearchMode = Literal["profile", "search"]
PostKindFilter = Literal["posts", "reposts", "all"]
DEFAULT_ACTION_INTERVAL_SECONDS = 1.0
KEYWORD_SEPARATOR_PATTERN: Final = re.compile(r"[,、，\n]")


def normalize_keyword_text(text: str) -> str:
    """全角/半角と大文字/小文字の違いを無視して比較できる形にする。"""
    return unicodedata.normalize("NFKC", text).casefold()


def parse_keywords(text: str) -> tuple[str, ...]:
    """カンマ区切りのキーワード入力を、空要素と重複を除いたタプルにする。"""
    keywords: list[str] = []
    for part in KEYWORD_SEPARATOR_PATTERN.split(text):
        keyword = part.strip()
        if keyword and keyword not in keywords:
            keywords.append(keyword)
    return tuple(keywords)


def text_contains_any_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    normalized_text = normalize_keyword_text(text)
    return any(normalize_keyword_text(keyword) in normalized_text for keyword in keywords)


@dataclass(frozen=True, slots=True)
class PostRecord:
    id: str
    url: str
    author_username: str
    text: str
    date: str
    likes: int
    replies: int
    has_media: bool
    is_reply: bool
    is_repost: bool


@dataclass(frozen=True, slots=True)
class PostActionTarget:
    url: str
    is_repost: bool


@dataclass(frozen=True, slots=True)
class PostActionResult:
    target: PostActionTarget
    action_label: str
    success: bool
    error_message: str | None


@dataclass(slots=True, frozen=True)
class ExecuteActionsRequest:
    targets: list[PostActionTarget]
    interval_seconds: float = DEFAULT_ACTION_INTERVAL_SECONDS

    def validate(self) -> None:
        if not self.targets:
            raise ValueError(tr("実行対象が選択されていません。"))
        if self.interval_seconds < 0:
            raise ValueError(tr("削除/解除間隔は0秒以上で指定してください。"))


@dataclass(slots=True, frozen=True)
class CollectRequest:
    username: str
    max_posts: int
    media_filter: MediaFilter
    is_reply: bool
    min_likes: int
    min_replies: int
    search_mode: SearchMode
    post_kind_filter: PostKindFilter = "posts"
    since_date: date | None = None
    until_date: date | None = None
    include_keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.username.strip():
            raise ValueError(tr("アカウントIDを入力してください。"))
        if self.max_posts < 1:
            raise ValueError(tr("取得上限件数は1以上で指定してください。"))
        if self.min_likes < 0 or self.min_replies < 0:
            raise ValueError(tr("最低いいね数と最低返信数は0以上で指定してください。"))
        if self.media_filter not in {"all", "with_media", "without_media"}:
            raise ValueError(tr("不正なメディア条件が指定されました。"))
        if self.search_mode not in {"profile", "search"}:
            raise ValueError(tr("収集モードは profile または search を指定してください。"))
        if self.post_kind_filter not in {"posts", "reposts", "all"}:
            raise ValueError(tr("対象種別は posts / reposts / all のいずれかで指定してください。"))
        if self.since_date is not None and not isinstance(self.since_date, date):
            raise ValueError(tr("開始日は YYYY-MM-DD 形式の日付で指定してください。"))
        if self.until_date is not None and not isinstance(self.until_date, date):
            raise ValueError(tr("終了日は YYYY-MM-DD 形式の日付で指定してください。"))
        if self.since_date is not None and self.until_date is not None and self.since_date > self.until_date:
            raise ValueError(tr("開始日は終了日以前で指定してください。"))
        if any(not keyword.strip() for keyword in (*self.include_keywords, *self.exclude_keywords)):
            raise ValueError(tr("空のキーワードは指定できません。"))
