│        Suspect        │                                 Reason                                 │
├───────────────────────┼────────────────────────────────────────────────────────────────────────┤
│ contributors silently │ Beta API may require at least one author ID despite docs saying        │
│  required             │ optional                                                               │
├───────────────────────┼────────────────────────────────────────────────────────────────────────┤
│ english_title slug    │ Another post with same slug exists somewhere, API returns integer      │
│ conflict              │ error (odd but seen in DRF custom validators)                          │
├───────────────────────┼────────────────────────────────────────────────────────────────────────┤
│ Beta API bug          │ The /post/ endpoint on cms-beta may have a broken serializer field —   │
│                       │ category endpoint works because it's a different serializer            │
└───────────────────────┴───────────────────────────────────────────────────