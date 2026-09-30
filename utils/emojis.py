"""
이모지 상수 정의

전체 봇에서 사용하는 이모지를 중앙에서 관리합니다.
"""

# 공통 이모지
EMOJIS = {
    # 상태 표시
    'on': '✅',
    'off': '❌',
    'loading': '⏳',

    # 버튼
    'chart': '📊',
    'clock': '⏱️',
    'support': '💬',
    'invite': '🚀',
    'web': '🌐',
    'patch_note': '📝',
}

# 핑 상태 이모지 (동적)
PING_EMOJIS = {
    'good': '🟢',      # < 250ms
    'normal': '🟡',    # 250-400ms
    'bad': '🔴',       # > 400ms
}
