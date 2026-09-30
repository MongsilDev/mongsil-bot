"""
랭크 관련 공통 헬퍼 함수 모듈
"""
from typing import Optional, Dict, List
from client import ERClient
from utils.config import config
from utils.errors import APIError, InvalidUidError, NotFoundError
from utils.logging_config import get_logger
from utils import rank_history

logger = get_logger('rank_helpers')

# 랭킹 목록과 이터컷 기준 서버
RANKING_SERVER = 10
SERVER_NAMES = {10: "아시아1", 12: "북미", 13: "유럽", 14: "남미", 17: "아시아2", 18: "아시아3"}


async def fetch_user_stats_solo(
    client: ERClient,
    user_id: str,
    season_id: int,
    use_cache: bool = True
) -> Optional[Dict]:
    """
    유저의 솔로 랭크 통계를 가져옵니다.

    Args:
        client: ERClient 인스턴스
        user_id: 유저 ID
        season_id: 시즌 ID
        use_cache: 캐시 사용 여부 (기본값: True)

    Returns:
        유저의 솔로 랭크 통계 딕셔너리 또는 None

    Raises:
        APIError: API 요청 실패 시
        NotFoundError: 통계가 없을 시
    """
    try:
        # v1을 v2로 교체하여 사용
        api_base = config.api_url.replace('/v1', '/v2')
        url = f"{api_base}/user/stats/uid/{user_id}/{season_id}/3"

        data = await client.api_client.get(url, use_cache=use_cache, ttl=300)

        if data and data.get('code') == 200:
            stats_list = data.get('userStats', [])
            if not stats_list:
                # uid까지 찾힌 유저라 닉네임 문제는 아니다
                raise NotFoundError("유저 통계 없음", "이번 시즌 랭크 게임을 한 판 이상 한 유저만 조회할 수 있습니다.")

            # 랭크 솔로 모드 통계 찾기 (matchingMode=3, matchingTeamMode=3)
            for stats in stats_list:
                if (stats.get('matchingMode', 0) == 3 and
                    stats.get('matchingTeamMode', 0) == 3):
                    return stats

            # matchingMode 필터가 맞지 않으면 첫 번째 결과 반환 (API 응답 형식 변경 대응)
            if len(stats_list) == 1:
                return stats_list[0]

            # 통계가 있는데 랭크 모드 항목만 없는 건 유저 조건이 아니라 응답 형식 이상.
            raise APIError("유저 통계 형식 불일치: 랭크 모드 항목 없음",
                           "통계를 불러오지 못했습니다. 잠시 후 다시 시도해주세요.")
        else:
            error_msg = data.get('message') if data else 'No response'
            # 탈퇴한 계정은 닉네임 검색 인덱스에 남아 uid 조회는 되지만
            # 통계 조회가 User Not Found로 떨어진다. 유저 조건이지 장애가 아니다.
            # 저장해 둔 uid가 무효가 되면 401 Unauthorized
            if error_msg in ('User Not Found', 'Unauthorized'):
                logger.warning(f"유저 통계 {error_msg}, uid={user_id}")
                raise InvalidUidError(
                    f"유저 통계 없음(uid 무효): {user_id}",
                    "유저 정보를 찾을 수 없습니다. 닉네임을 바꿨다면 새 닉네임으로 조회해주세요."
                )
            logger.error(f"유저 통계 API 오류: {error_msg} (uid={user_id})")
            raise APIError(f"API 오류: {error_msg}", "API 요청 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
    except (APIError, NotFoundError):
        raise
    except Exception as e:
        logger.error(f"유저 통계 조회 중 오류 발생: {e}", exc_info=True)
        raise APIError(f"네트워크 오류: {e}", "네트워크 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")


async def fetch_ranking_data(client: ERClient, season_id: int, use_cache: bool = True) -> Optional[List[Dict]]:
    """
    시즌 랭킹 데이터를 가져옵니다.

    Args:
        client: ERClient 인스턴스
        season_id: 시즌 ID
        use_cache: 캐시 사용 여부 (기본값: True)

    Returns:
        랭킹 데이터 리스트 또는 None
    """
    try:
        url = f"{config.api_url}/rank/top/{season_id}/3/{RANKING_SERVER}"

        data = await client.api_client.get(url, use_cache=use_cache, ttl=300)

        if data and data.get('code') == 200:
            top_ranks = data.get('topRanks', [])
            logger.debug(f"랭킹 데이터 {len(top_ranks)}명 조회 완료")
            rank_history.record_ranks(season_id, top_ranks[:100])
            return top_ranks
        else:
            error_msg = data.get('message') if data else 'No response'
            logger.error(f"랭킹 API 오류: {error_msg}")
            return None
    except Exception as e:
        logger.error(f"랭킹 API 호출 중 오류 발생: {e}", exc_info=True)
        return None


async def fetch_user_rank(client: ERClient, user_id: str, season_id: int) -> Optional[Dict]:
    """
    유저의 서버 순위를 가져옵니다. 이터니티와 데미갓은 이 serverRank로 판정합니다.

    Returns:
        userRank 딕셔너리, 실패 시 None
    """
    url = f"{config.api_url}/rank/uid/{user_id}/{season_id}/3"
    try:
        data = await client.api_client.get(url, ttl=300)
    except APIError as e:
        logger.warning(f"서버 순위 조회 실패, 통합 순위로 대체: {e.message}")
        return None

    if data and data.get('code') == 200:
        return data.get('userRank')
    logger.warning(f"서버 순위 응답 이상: {data.get('message') if data else 'No response'}")
    return None
