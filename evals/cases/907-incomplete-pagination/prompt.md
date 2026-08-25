# Adversarial case 907 — incomplete bounded API simulation

The roads source's first WFS/REST page is filled to its request limit
(`page_size: 1`) but the service-declared total (`matched: 5`) exceeds what
was actually returned (`returned: 1`). A page filled to the limit is not
proof of completeness — the agent must page further. This case asserts
`provenance.bounded_api_completeness` catches the gap.
