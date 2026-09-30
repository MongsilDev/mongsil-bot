"""
최적화된 API 클라이언트 유틸리티
"""
import aiohttp
import asyncio
from typing import Any, Optional, Dict
from datetime import datetime, timedelta
from collections import OrderedDict
from .config import config
from .errors import APIError, BotError, redact_secrets
from .logging_config import get_logger

logger = get_logger('api_client')

class OptimizedAPIClient:
    """최적화된 API 클라이언트"""

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._cache_times: OrderedDict[str, datetime] = OrderedDict()
        self._max_cache_size = 1000
        self._semaphore = asyncio.Semaphore(10)  # 동시 요청 제한
        
    async def get_session(self) -> aiohttp.ClientSession:
        """세션을 가져오거나 생성합니다."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            connector = aiohttp.TCPConnector(
                limit=100,  # 총 연결 수 제한
                limit_per_host=30,  # 호스트당 연결 수 제한
                ttl_dns_cache=300,  # DNS 캐시 TTL
                use_dns_cache=True,
            )
            
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
            )
        return self._session
    
    async def close(self):
        """세션을 닫습니다."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
    
    def _get_cache_key(self, url: str, params: Optional[Dict] = None) -> str:
        """캐시 키를 생성합니다."""
        if params:
            sorted_params = sorted(params.items())
            param_str = "&".join(f"{k}={v}" for k, v in sorted_params)
            return f"{url}?{param_str}"
        return url
    
    def _is_cache_valid(self, cache_key: str, ttl: int) -> bool:
        """캐시가 유효한지 확인합니다."""
        if cache_key not in self._cache:
            return False

        cache_time = self._cache_times.get(cache_key)
        if not cache_time:
            return False

        return datetime.now() - cache_time < timedelta(seconds=ttl)
    
    def uncache(self, url: str, params: Optional[Dict] = None):
        """특정 요청의 캐시를 지운다. 부정 응답을 장기 캐시하지 않기 위한 용도."""
        cache_key = self._get_cache_key(url, params)
        self._cache.pop(cache_key, None)
        self._cache_times.pop(cache_key, None)

    def _set_cache(self, cache_key: str, data: Any):
        """캐시를 설정합니다. (LRU)"""
        # 기존 키가 있으면 삭제 (재정렬을 위해)
        if cache_key in self._cache:
            del self._cache[cache_key]
            del self._cache_times[cache_key]

        # 새로 추가 (OrderedDict의 끝에 추가됨)
        self._cache[cache_key] = data
        self._cache_times[cache_key] = datetime.now()

        # 캐시 크기 제한 (LRU - 가장 오래된 항목 제거)
        if len(self._cache) > self._max_cache_size:
            # OrderedDict의 첫 번째 항목이 가장 오래된 항목
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]
            del self._cache_times[oldest_key]
    
    async def get(self, url: str, params: Optional[Dict] = None, use_cache: bool = True, ttl: Optional[int] = None,
                  refresh: bool = False) -> Dict[str, Any]:
        """GET 요청을 수행합니다. ttl은 이 요청의 캐시 유효 시간(초), 없으면 config.cache_ttl. refresh면 캐시를 건너뛰고 새 값으로 채움."""
        MAX_RETRIES = 3

        cache_key = self._get_cache_key(url, params)

        # 캐시 확인
        if use_cache and not refresh and self._is_cache_valid(cache_key, ttl or config.cache_ttl):
            # LRU: 최근 사용 항목으로 이동
            self._cache.move_to_end(cache_key)
            return self._cache[cache_key]

        # 429 대기는 세마포어 밖에서 한다. 슬롯을 쥔 채 재시도를 기다리면
        # 동시 429 시 전 요청이 슬롯을 물고 서로를 기다리는 교착이 된다.
        for attempt in range(MAX_RETRIES + 1):
            retry_after = None

            async with self._semaphore:
                session = await self.get_session()

                # API 키는 bser 요청에만 실어 다른 호스트(Steam, 동물 API 등)로 새지 않게 한다
                headers = {'x-api-key': config.api_key} if url.startswith('https://open-api.bser.io') else None

                try:
                    async with session.get(url, params=params, headers=headers) as response:
                        if response.status == 200:
                            data = await response.json()
                            if use_cache:
                                self._set_cache(cache_key, data)
                            return data
                        elif response.status == 429:
                            if attempt >= MAX_RETRIES:
                                raise APIError(
                                    f"API Rate limit 초과 (최대 {MAX_RETRIES}회 재시도 후 실패)",
                                    "API 요청 한도를 초과했습니다. 잠시 후 다시 시도해주세요."
                                )
                            # Retry-After는 HTTP-date 형식일 수 있어 int 실패 시 기본값 사용
                            try:
                                retry_after = int(response.headers.get('Retry-After', config.retry_delay))
                            except ValueError:
                                retry_after = config.retry_delay
                            logger.warning(f"API Rate limit - {retry_after}초 대기 (재시도 {attempt + 1}/{MAX_RETRIES})")
                        else:
                            error_text = await response.text()
                            logger.warning(f"API 요청 실패: {response.status}")
                            raise APIError(
                                f"API 요청 실패: {response.status} - {error_text}",
                                "API 요청 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
                            )
                except aiohttp.ClientError as e:
                    logger.warning(f"네트워크 오류: {redact_secrets(e)}")
                    raise APIError(f"네트워크 오류: {redact_secrets(e)}", "네트워크 오류가 발생했습니다. 잠시 후 다시 시도해주세요.") from None
                except asyncio.TimeoutError:
                    # str()이 빈 문자열이라 일반 분기로 가면 "예상치 못한 오류: "만 남는다
                    logger.warning(f"API 요청 시간 초과: {url}")
                    raise APIError("요청 시간 초과", "응답이 늦어지고 있습니다. 잠시 후 다시 시도해주세요.") from None
                except (APIError, BotError):
                    raise
                except Exception as e:
                    logger.error(f"예상치 못한 오류: {type(e).__name__}: {redact_secrets(e)}")
                    raise BotError(f"예상치 못한 오류: {type(e).__name__}: {redact_secrets(e)}", "서버 오류가 발생했습니다. 잠시 후 다시 시도해주세요.") from None

            await asyncio.sleep(retry_after)
    
# 전역 API 클라이언트 인스턴스
api_client = OptimizedAPIClient()
