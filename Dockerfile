# ── Synology DS218+ 최적화 Dockerfile ──────────────────
# DS218+: Intel Celeron J3355 (x86_64)
# bullseye 는 2026-09-08 자로 보안 저장소 Release 가 만료돼 apt-get update 가
# 통째로 실패한다. 파이썬 버전은 3.11 그대로 두고 베이스만 bookworm 으로 올린다.
FROM python:3.11-slim-bookworm

# 시스템 의존성
RUN apt-get update && apt-get install -y \
    gcc \
    libffi-dev \
    libssl-dev \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 타임존 설정 (KST)
ENV TZ=Asia/Seoul
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# 의존성 먼저 복사 (캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 소스 복사
COPY . .

# 로그/데이터 디렉터리 생성
# ※ botuser 제거: Synology NAS 볼륨 마운트 시 권한 충돌 방지
RUN mkdir -p logs data

CMD ["python", "-u", "main.py"]