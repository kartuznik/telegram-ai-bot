# Редакторские качества Кузьмы (канал / редактор контента)

Ниже — **15 качеств**, которые усиливают посты перед публикацией, и где они реализованы в коде.

| # | Качество | Суть | Реализация |
|---|----------|------|------------|
| 1 | **Перекрёстные источники** | Факты сверяются с доп. выдачей Tavily с других доменов, не только с основным URL. | `LLMAgent.gather_cross_reference_for_primary`, промпт в `draft_post_from_snippet` (`content_editor.py`). |
| 2 | **Оценка уверенности** | Число 0–100 после фактчека по черновику и блоку источников. | `LLMAgent.editorial_factcheck_scores` → поле `draft_posts.confidence_score`. |
| 3 | **Флаг ручной проверки** | Явный сигнал, что пост стоит перечитать перед каналом. | `requires_verification` в БД; форсируется при &lt;2 доменах, противоречиях, сбое парсинга JSON. |
| 4 | **Детекция противоречий** | Модель помечает расхождения между источниками или текстом и сниппетами. | JSON-поле `contradiction` в `editorial_factcheck_scores`; штраф к confidence и `needs_review`. |
| 5 | **Устойчивый парсинг фактчека** | Ответ может быть в markdown-блоке «json», с префиксом/суффиксом — берётся подстрока от первого `{` до последнего `}`. | `LLMAgent._parse_factcheck_json_response`; fallback 50 / review / no contradiction + `warning` в лог. |
| 6 | **SEO: длина заголовка** | Первая строка черновика 40–60 символов — целевой коридор для Telegram. | `_seo_score_for_title` (`content_editor.py`), влияет на `seo_score`. |
| 7 | **SEO: ключевые слова тем** | Заголовок должен пересекаться с темами пользователя (`/topics`). | Те же `_topic_keyword_hints` / `_seo_score_for_title`. |
| 8 | **Сохранение SEO в БД** | Метрика видна в ЛС и в аналитике, не только в рантайме. | `draft_posts.seo_score`. |
| 9 | **Приоритет «SEO-дружелюбных» материалов** | Кандидаты из поиска с удачным заголовком получают +0.5 к эффективному скору. | `_pick_draft_item`: `seo_bonus_pick` к `effective_score`. |
| 10 | **Расширение окна по датам** | При пустой выдаче Tavily второй запрос с большим `days` (например 2→5). | `tavily_fallback_days_after_empty`, `_pick_draft_item`. |
| 11 | **Третья попытка поиска** | Упрощённый/расширенный запрос и до 7+ дней, если после двух проходов нет веб-кандидатов. | `_broaden_tavily_news_query`, третий `_tavily_search`. |
| 12 | **Счётчик неудач подбора** | Серия пустых подборов без создания черновика. | `PREF_PICK_FAIL_STREAK`, `_pick_fail_streak_*`. |
| 13 | **Сообщение об исчерпании темы** | После 3 неудач — подсказка сменить тему (`/topics`). | `_exhaustion_message_suffix` в ответах `create_draft_from_search`. |
| 14 | **Прозрачность в ЛС** | Пользователь видит confidence, SEO и флаг проверки над текстом черновика. | `draft_dm_text` (`content_editor.py`). |
| 15 | **Чистый текст в канале** | Служебные строки не лезут в опубликованный пост. | `channel_publish_text_from_draft_body`, набор `_EDITOR_SERVICE_LINES_EXACT`. |

## Связанные файлы

- `app/llm_agent.py` — Tavily cross-ref, `editorial_factcheck_scores`, парсинг JSON.  
- `app/content_editor.py` — подбор, SEO, черновик, streak, DM.  
- `app/database.py` — миграции `draft_posts` (`confidence_score`, `requires_verification`, `seo_score`).  

## Логи

Полезные маркеры при отладке: `Draft metrics`, `editorial_factcheck_scores`, `Pick draft`, `Tavily`.

```bash
journalctl -u kuzya-bot -n 80 | grep -iE 'confidence|seo|factcheck|Draft metrics'
```
