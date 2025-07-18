#!/usr/bin/env python3
import json
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .github_utils import *
from .theme_manager import ThemeManager

ERROR_MESSAGES = {
    401: "❌ 인증 실패: 잘못된 GitHub 토큰입니다. 토큰 값을 확인해 주세요.",
    403: ("⚠️ 요청 실패 (403): GitHub API rate limit에 도달했습니다.\n"
          "🔑 토큰 없이 실행하면 1시간에 최대 60회 요청만 허용됩니다.\n"
          "💡 해결법: --token 옵션으로 GitHub 개인 액세스 토큰을 입력해 주세요."),
    404: "⚠️ 요청 실패 (404): 리포지토리가 존재하지 않습니다.",
    500: "⚠️ 요청 실패 (500): GitHub 내부 서버 오류 발생!",
    503: "⚠️ 요청 실패 (503): 서비스 불가",
    422: ("⚠️ 요청 실패 (422): 처리할 수 없는 컨텐츠\n"
          "⚠️ 유효성 검사에 실패 했거나, 엔드 포인트가 스팸 처리되었습니다.")
}

logger = logging.getLogger(__name__)


def get_emoji(score):
    if score >= 90:
        return "🌟"  # 최상위 성과
    elif score >= 80:
        return "⭐"  # 탁월한 성과
    elif score >= 70:
        return "🎯"  # 목표 달성
    elif score >= 60:
        return "🎨"  # 양호한 성과
    elif score >= 50:
        return "🌱"  # 성장 중
    elif score >= 40:
        return "🍀"  # 발전 가능성
    elif score >= 30:
        return "🌿"  # 초기 단계
    elif score >= 20:
        return "🍂"  # 개선 필요
    elif score >= 10:
        return "🍁"  # 참여 시작
    else:
        return "🌑"  # 최소 참여


class RepoAnalyzer:
    """Class to analyze repository participation for scoring"""
    # 점수 가중치
    SCORE_WEIGHTS = {
        'feat_bug_pr': 3,
        'doc_pr': 2,
        'typo_pr': 1,
        'feat_bug_is': 2,
        'doc_is': 1
    }

    # 사용자 제외 목록
    EXCLUDED_USERS = {"kyahnu", "kyagrd"}

    def __init__(self, repo_path: str, theme: str = 'default', dry_run: bool = False):  # token 파라미터 제거 
        # 테스트용 저장소나 통합 분석용 저장소 식별
        self._is_test_repo = repo_path == "dummy/repo"
        self._is_multiple_repos = repo_path == "multiple_repos"
        self.dry_run = dry_run

        # 테스트용이나 통합 분석용이 아닌 경우에만 실제 저장소 존재 여부 확인
        if not self._is_test_repo and not self._is_multiple_repos:
            if not check_github_repo_exists(repo_path):  # 토큰 파라미터 제거
                logger.error(f"입력한 저장소 '{repo_path}'가 GitHub에 존재하지 않습니다.")
                sys.exit(1)
        elif self._is_test_repo:
            logger.debug(f"ℹ️ [TEST MODE] '{repo_path}'는 테스트용 저장소로 간주합니다.")
        elif self._is_multiple_repos:
            logger.debug(f"ℹ️ [통합 분석] 여러 저장소의 통합 분석을 수행합니다.")

        self.repo_path = repo_path
        self.participants: dict[str, dict[str, int]] = {}
        self.weekly_activity = defaultdict(lambda: {'pr': 0, 'issue': 0})
        self.semester_start_date = None

        self.score = self.SCORE_WEIGHTS.copy()

        self.theme_manager = ThemeManager()  # 테마 매니저 초기화
        self.set_theme(theme)  # 테마 설정

        self._data_collected = True
        self.__previous_create_at = None

        # 환경변수에서 토큰을 읽어서 세션 설정
        self.SESSION = requests.Session()
        token = os.getenv('GITHUB_TOKEN')
        if token:
            self.SESSION.headers.update({
                'Authorization': f'Bearer {token}',
                'Accept': 'application/vnd.github+json',
                'User-Agent': 'reposcore-py'
            })
        else:
            # 토큰이 없어도 표준 헤더는 설정
            self.SESSION.headers.update({
                'Accept': 'application/vnd.github+json',
                'User-Agent': 'reposcore-py'
            })

    @property
    def previous_create_at(self) -> int | None:
        if self.__previous_create_at is None:
            return None
        else:
            return int(self.__previous_create_at.timestamp())

    @previous_create_at.setter
    def previous_create_at(self, value):
        self.__previous_create_at = datetime.fromtimestamp(value, tz=timezone.utc)

    def set_theme(self, theme_name: str) -> None:
        if theme_name in self.theme_manager.themes:
            self.theme_manager.current_theme = theme_name
        else:
            raise ValueError(f"지원하지 않는 테마입니다: {theme_name}")

    def _handle_api_error(self, status_code: int) -> bool:
        if status_code in ERROR_MESSAGES:
            logger.error(ERROR_MESSAGES[status_code])
            self._data_collected = False
            return True
        elif status_code != 200:
            logger.warning(f"⚠️ GitHub API 요청 실패: {status_code}")
            self._data_collected = False
            return True
        return False

    def collect_PRs_and_issues(self) -> None:
        """
        하나의 API 호출로 GitHub 이슈 목록을 가져오고,
        pull_request 필드가 있으면 PR로, 없으면 issue로 간주.
        PR의 경우, 실제로 병합된 경우만 점수에 반영.
        이슈는 open / reopened / completed 상태만 점수에 반영합니다.
        """
        if self.dry_run:
            logger.debug(f"[DRY-RUN] '{self.repo_path}'에 대해 PR/이슈 수집을 생략합니다.")
            return
        # 테스트용 저장소나 통합 분석용인 경우 API 호출을 건너뜁니다
        if self._is_test_repo:
            logger.info(f"ℹ️ [TEST MODE] '{self.repo_path}'는 테스트용 저장소입니다. 실제 GitHub API 호출을 수행하지 않습니다.")
            return
        elif self._is_multiple_repos:
            logger.info(f"ℹ️ [통합 분석] 통합 분석을 위한 저장소입니다. API 호출을 건너뜁니다.")
            return

        page = 1
        per_page = 100

        while True:
            url = f"https://api.github.com/repos/{self.repo_path}/issues"

            response = retry_request(self.SESSION,
                                     url,
                                     params={
                                         'state': 'all',
                                         'per_page': per_page,
                                         'page': page
                                     })

            # 🔽 에러 처리 부분 25줄 → 3줄로 리팩토링
            if self._handle_api_error(response.status_code):
                return

            items = response.json()
            if not items:
                break

            for item in items:
                if 'created_at' not in item:
                    logger.warning(f"⚠️ 요청 분석 실패")
                    return

                server_create_datetime = datetime.fromisoformat(item['created_at'])

                if self.semester_start_date:
                    created_date = server_create_datetime.astimezone(ZoneInfo("Asia/Seoul")).date()
                    week_index = (created_date - self.semester_start_date).days // 7 + 1
                    if 'pull_request' in item and item.get('pull_request', {}).get('merged_at'):
                        self.weekly_activity[week_index]['pr'] += 1
                    elif item.get('state_reason') in ('completed', 'reopened', None):
                        self.weekly_activity[week_index]['issue'] += 1

                self.__previous_create_at = server_create_datetime if self.__previous_create_at is None else max(self.__previous_create_at,server_create_datetime)

                author = item.get('user', {}).get('login', 'Unknown')
                if author not in self.participants:
                    self.participants[author] = {
                        'p_enhancement': 0,
                        'p_bug': 0,
                        'p_documentation': 0,
                        'p_typo': 0,
                        'i_enhancement': 0,
                        'i_bug': 0,
                        'i_documentation': 0,
                    }

                labels = item.get('labels', [])
                label_names = [label.get('name', '') for label in labels if label.get('name')]

                state_reason = item.get('state_reason')

                # PR 처리 (병합된 PR만)
                if 'pull_request' in item:
                    # 이슈 객체에서 PR 번호 꺼내기
                    pr_number = item.get('number')
                    if pr_number is None:
                        continue
                    
                    if 'pull_request' in item:
                        pr_number = item.get('number')
                        if pr_number is None:
                            continue

                        merged = False

                        merged_at_inline = item.get('pull_request', {}).get('merged_at')
                        if merged_at_inline:
                            merged = True

                    elif item.get('state') == 'closed':
                        pr_url = f"https://api.github.com/repos/{self.repo_path}/pulls/{pr_number}"
                        try:
                            pr_resp = retry_request(self.SESSION, pr_url)
                        except Exception:
                            continue
                        if self._handle_api_error(pr_resp.status_code):
                            continue
                        pr_data = pr_resp.json()
                        merged = bool(pr_data.get('merged_at'))
                        
                    if merged:
                        # JS와 동일하게 첫 번째 라벨만 사용
                        if label_names:
                            first_label = label_names[0]
                            if first_label in ['enhancement', 'bug']:
                                self.participants[author]['p_enhancement'] += 1
                            elif first_label == 'documentation':
                                self.participants[author]['p_documentation'] += 1
                            elif first_label == 'typo':
                                self.participants[author]['p_typo'] += 1

                # 이슈 처리 (open / reopened / completed 만 포함, not planned 제외)
                else:
                    if state_reason in ('completed', 'reopened', None):
                        # JS와 동일하게 첫 번째 라벨만 사용
                        if label_names:  # 라벨이 존재하는 경우만
                            first_label = label_names[0]  # 첫 번째 라벨만 선택
                            if first_label in ['enhancement', 'bug']:
                                self.participants[author]['i_enhancement'] += 1
                            elif first_label == 'documentation':
                                self.participants[author]['i_documentation'] += 1

            # 다음 페이지 검사
            link_header = response.headers.get('link', '')
            if 'rel="next"' in link_header:
                page += 1
            else:
                break

        if not self.participants:
            logger.warning("⚠️ 수집된 데이터가 없습니다. (참여자 없음)")
            logger.info("📄 참여자는 없지만, 결과 파일은 생성됩니다.")
        else:
            self.participants = {
                user: info for user, info in self.participants.items()
                if user not in self.EXCLUDED_USERS
            }
            logger.debug("\n참여자별 활동 내역 (participants 딕셔너리):")
            for user, info in self.participants.items():
                logger.debug(f"{user}: {info}")

    def _extract_pr_counts(self, activities: dict) -> tuple[int, int, int, int, int]:
        """PR 관련 카운트 추출"""
        p_f = activities.get('p_enhancement', 0)
        p_b = activities.get('p_bug', 0)
        p_d = activities.get('p_documentation', 0)
        p_t = activities.get('p_typo', 0)
        p_fb = p_f + p_b
        return p_f, p_b, p_d, p_t, p_fb

    def _extract_issue_counts(self, activities: dict) -> tuple[int, int, int, int]:
        """이슈 관련 카운트 추출"""
        i_f = activities.get('i_enhancement', 0)
        i_b = activities.get('i_bug', 0)
        i_d = activities.get('i_documentation', 0)
        i_fb = i_f + i_b
        return i_f, i_b, i_d, i_fb

    def _calculate_valid_counts(self, p_fb: int, p_d: int, p_t: int, i_fb: int, i_d: int) -> tuple[int, int]:
        """유효한 카운트 계산"""
        p_valid = p_fb + min(p_d + p_t, 3 * max(p_fb, 1))
        i_valid = min(i_fb + i_d, 4 * p_valid)
        return p_valid, i_valid

    def _calculate_adjusted_counts(self, p_fb: int, p_d: int, p_valid: int, i_fb: int, i_valid: int) -> tuple[int, int, int, int, int]:
        """조정된 카운트 계산"""
        p_fb_at = min(p_fb, p_valid)
        p_d_at = min(p_d, p_valid - p_fb_at)
        p_t_at = p_valid - p_fb_at - p_d_at
        i_fb_at = min(i_fb, i_valid)
        i_d_at = i_valid - i_fb_at
        return p_fb_at, p_d_at, p_t_at, i_fb_at, i_d_at

    def _calculate_total_score(self, p_fb_at: int, p_d_at: int, p_t_at: int, i_fb_at: int, i_d_at: int) -> int:
        """총점 계산"""
        return (
                self.score['feat_bug_pr'] * p_fb_at +
                self.score['doc_pr'] * p_d_at +
                self.score['typo_pr'] * p_t_at +
                self.score['feat_bug_is'] * i_fb_at +
                self.score['doc_is'] * i_d_at
        )

    def _create_score_dict(self, p_fb_at: int, p_d_at: int, p_t_at: int, i_fb_at: int, i_d_at: int, total: int) -> dict[str, float]:
        """점수 딕셔너리 생성"""
        return {
            "feat/bug PR": self.score['feat_bug_pr'] * p_fb_at,
            "document PR": self.score['doc_pr'] * p_d_at,
            "typo PR": self.score['typo_pr'] * p_t_at,
            "feat/bug issue": self.score['feat_bug_is'] * i_fb_at,
            "document issue": self.score['doc_is'] * i_d_at,
            "total": total
        }

    def _finalize_scores(self, scores: dict, total_score_sum: float, user_info: dict | None = None) -> dict[str, dict[str, float]]:
        """최종 점수 계산 및 정렬"""
        # 비율 계산
        for participant in scores:
            total = scores[participant]["total"]
            rate = (total / total_score_sum) * 100 if total_score_sum > 0 else 0
            scores[participant]["rate"] = round(rate, 1)

        # 사용자 정보 매핑 (제공된 경우)
        if user_info:
            new_scores = {}
            for k in list(scores.keys()):
                display_name = user_info.get(k, k)
                new_scores[display_name] = scores[k]
            scores = new_scores


        sorted_items = sorted(scores.items(), key=lambda x: x[1]["total"], reverse=True)

        # 공동 등수 처리
        ranked_scores = {}
        last_score = None
        current_rank = 0
        rank_counter = 0

        for user, data in sorted_items:
            rank_counter += 1
            if data["total"] != last_score:
                current_rank = rank_counter
                last_score = data["total"]
            data["rank"] = current_rank
            ranked_scores[user] = data

        return ranked_scores

    def calculate_scores(self, user_info: dict[str, str] | None = None, min_contributions: int = 0) -> dict[str, dict[str, float]]:
        """참여자별 점수 계산"""
        scores = {}
        total_score_sum = 0

        for participant, activities in self.participants.items():
            # PR 카운트 추출
            p_f, p_b, p_d, p_t, p_fb = self._extract_pr_counts(activities)

            # 이슈 카운트 추출
            i_f, i_b, i_d, i_fb = self._extract_issue_counts(activities)

            # 유효 카운트 계산
            p_valid, i_valid = self._calculate_valid_counts(p_fb, p_d, p_t, i_fb, i_d)

            # ✅ PR 0개인데 이슈만 있는 경우 1:4 규칙 보정
            if p_fb == 0 and p_d == 0 and p_t == 0 and (i_fb + i_d) > 0:
                # PR은 없지만, 이슈를 위해 PR 1개 있다고 간주 (계산용)
                p_valid = 1
                i_valid = min(i_fb + i_d, 4 * p_valid)

                # 💡 실제 PR 점수는 0으로 고정
                p_fb_at = 0
                p_d_at = 0
                p_t_at = 1 if p_t > 0 else 0  # typo 1개 있으면 점수 부여
                i_fb_at = min(i_fb, i_valid)
                i_d_at = i_valid - i_fb_at

                total = (
                        self.score['feat_bug_is'] * i_fb_at +
                        self.score['doc_is'] * i_d_at +
                        self.score['typo_pr'] * p_t_at
                )

                scores[participant] = {
                    "feat/bug PR": 0.0,
                    "document PR": 0.0,
                    "typo PR": 0.0,
                    "feat/bug issue": self.score['feat_bug_is'] * i_fb_at,
                    "document issue": self.score['doc_is'] * i_d_at,
                    "total": total
                }

                total_score_sum += total
                continue

            # 조정된 카운트 계산
            p_fb_at, p_d_at, p_t_at, i_fb_at, i_d_at = self._calculate_adjusted_counts(
                p_fb, p_d, p_valid, i_fb, i_valid
            )

            # 총점 계산
            total = self._calculate_total_score(p_fb_at, p_d_at, p_t_at, i_fb_at, i_d_at)

            scores[participant] = self._create_score_dict(p_fb_at, p_d_at, p_t_at, i_fb_at, i_d_at, total)
            total_score_sum += total

        if min_contributions > 0:
            scores = {user: s for user, s in scores.items() if s["total"] >= min_contributions}

        return self._finalize_scores(scores, total_score_sum, user_info)

    def set_semester_start_date(self, date: datetime.date) -> None:
        """--semester-start 옵션에서 받은 학기 시작일 저장"""
        self.semester_start_date = date

    def calculate_averages(self, scores: dict[str, dict[str, float]]) -> dict[str, float]:
        """점수 딕셔너리에서 각 카테고리별 평균을 계산합니다."""
        if not scores:
            return {"feat/bug PR": 0, "document PR": 0, "typo PR": 0, "feat/bug issue": 0, "document issue": 0, "total": 0, "rate": 0}

        num_participants = len(scores)
        totals = {
            "feat/bug PR": 0,
            "document PR": 0,
            "typo PR": 0,
            "feat/bug issue": 0,
            "document issue": 0,
            "total": 0
        }

        for participant, score_data in scores.items():
            for category in totals.keys():
                totals[category] += score_data[category]

        averages = {category: total / num_participants for category, total in totals.items()}
        total_rates = sum(score_data["rate"] for score_data in scores.values())
        averages["rate"] = total_rates / num_participants if num_participants > 0 else 0

        return averages

    def is_cache_update_required(self, cache_path: str) -> bool:
        """캐시 업데이트 필요 여부 확인"""
        if not os.path.exists(cache_path):
            return True

        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
                cached_timestamp = cache_data.get('update_time', 0)
                current_timestamp = int(datetime.now(timezone.utc).timestamp())
                return current_timestamp - cached_timestamp > 3600  # 1시간
        except (json.JSONDecodeError, KeyError):
            return True
