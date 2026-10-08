-- ============================================================
--  Agent 元数据库 (audit_agent)
--
--  与业务库 campus_market 分离：
--    业务库放平台数据（item / item_audit ...），属于平台职责
--    元数据库放 Agent 自己的任务、轨迹、评测，属于 Agent 职责
--  这样 Agent 换实现、加字段都不影响业务库。
-- ============================================================

DROP DATABASE IF EXISTS audit_agent;
CREATE DATABASE audit_agent CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE audit_agent;

-- ------------------------------------------------------------
-- 任务表，同时充当队列（SELECT ... FOR UPDATE SKIP LOCKED 抢占）
-- ------------------------------------------------------------
CREATE TABLE audit_jobs (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  item_id     BIGINT UNSIGNED NOT NULL,
  fingerprint VARCHAR(64) NULL COMMENT '内容指纹，用于幂等：内容没变就不重复审',
  status      VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending/running/succeeded/failed',
  verdict     VARCHAR(16) DEFAULT NULL COMMENT 'APPROVE/REJECT/REVIEW',
  confidence  DECIMAL(4,3) DEFAULT NULL,
  reason      TEXT,
  rule_hits   JSON DEFAULT NULL,
  source      VARCHAR(16) DEFAULT NULL COMMENT 'rule/llm/human',
  locked_by   VARCHAR(64) DEFAULT NULL,
  locked_at   DATETIME DEFAULT NULL,
  started_at  DATETIME DEFAULT NULL COMMENT 'worker 开始处理的时刻',
  duration_ms INT DEFAULT NULL COMMENT '处理耗时（不含排队等待）',
  attempts    INT NOT NULL DEFAULT 0,
  error       TEXT,
  -- ★ 活跃任务唯一键的载体（MySQL 没有「部分索引」，用生成列绕过去）
  --
  -- 需求：同一个「商品 + 内容指纹」最多只能有 **一个活跃任务**（pending/running），
  --       但历史任务（succeeded/failed）可以有任意多条 —— 那是审计记录，要留着。
  --
  -- 做法：活跃时生成列 = "itemId:fingerprint"，非活跃时为 NULL；
  --       而 MySQL 的唯一键**忽略 NULL**，于是天然只对活跃行生效。
  --
  -- ⚠ 这个键曾经只存在于开发机的库里、漏写进了建表文件。后果是：
  --   任何人拉下来建新库，并发投递同一内容的幂等就完全失效。
  --   这个不一致是 CI 抓出来的 —— 全新库上 6 个并发插入全都成功，本该只有 1 个。
  --   （教训：**建表文件必须能从一个空库跑出和应用一致的库**，否则项目只在自己机器上活着。）
  live_key    VARCHAR(128) GENERATED ALWAYS AS (
                IF(status IN ('pending', 'running'),
                   CONCAT(item_id, ':', IFNULL(fingerprint, '')), NULL)
              ) VIRTUAL COMMENT '活跃任务唯一键载体；非活跃为 NULL',
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_live (live_key),
  KEY idx_queue (status, created_at),
  KEY idx_item_id (item_id),
  KEY idx_idem (item_id, fingerprint, status),
  KEY idx_reclaim (status, locked_at),
  KEY idx_duration (status, duration_ms)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='审核任务表/队列';

-- ------------------------------------------------------------
-- 每步 trace，可完整回放
-- ------------------------------------------------------------
CREATE TABLE audit_steps (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  job_id            BIGINT UNSIGNED NOT NULL,
  step_no           INT NOT NULL,
  kind              VARCHAR(24) NOT NULL COMMENT 'load/rule/llm/tool_call/tool_result/verdict/writeback',
  payload           JSON NOT NULL,
  latency_ms        INT DEFAULT NULL,
  tokens            INT DEFAULT NULL COMMENT '总 token（兼容旧数据）',
  prompt_tokens     INT DEFAULT NULL,
  completion_tokens INT DEFAULT NULL,
  created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_job_step (job_id, step_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='执行轨迹';

-- ------------------------------------------------------------
-- LLM 限流与成本护栏（单行表）
--
-- 为什么放数据库而不是进程内：限流必须是**全局**的 ——
-- 一个进程内多线程、以及多个 worker 进程，加起来不能超过模型服务的速率。
-- 用 SELECT ... FOR UPDATE 保证原子性，多进程也安全。
-- ------------------------------------------------------------
CREATE TABLE llm_guard (
  id                TINYINT UNSIGNED NOT NULL DEFAULT 1,
  tokens            DECIMAL(12,4) NOT NULL DEFAULT 0 COMMENT '当前令牌数',
  refilled_at_ms    BIGINT NOT NULL DEFAULT 0 COMMENT '上次补充令牌的时间戳(ms)',
  day               DATE NOT NULL COMMENT '当前计费日',
  day_calls         INT NOT NULL DEFAULT 0,
  day_prompt_tokens BIGINT NOT NULL DEFAULT 0,
  day_output_tokens BIGINT NOT NULL DEFAULT 0,
  day_cost          DECIMAL(14,6) NOT NULL DEFAULT 0 COMMENT '当日累计花费',
  day_degraded      INT NOT NULL DEFAULT 0 COMMENT '当日被护栏降级的次数',
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='LLM 限流与预算';

-- 每日用量历史（跨天时归档，用于看成本趋势）
CREATE TABLE llm_usage_daily (
  day           DATE NOT NULL,
  calls         INT NOT NULL DEFAULT 0,
  prompt_tokens BIGINT NOT NULL DEFAULT 0,
  output_tokens BIGINT NOT NULL DEFAULT 0,
  cost          DECIMAL(14,6) NOT NULL DEFAULT 0,
  degraded      INT NOT NULL DEFAULT 0,
  updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (day)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='LLM 每日用量';

-- ------------------------------------------------------------
-- 断点恢复
-- ------------------------------------------------------------
CREATE TABLE checkpoints (
  job_id   BIGINT UNSIGNED NOT NULL,
  step_no  INT NOT NULL,
  state    JSON NOT NULL,
  saved_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (job_id, step_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='断点';

-- ------------------------------------------------------------
-- 人工复核队列
-- ------------------------------------------------------------
CREATE TABLE review_queue (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  job_id     BIGINT UNSIGNED NOT NULL,
  item_id    BIGINT UNSIGNED NOT NULL,
  reason     VARCHAR(500) NOT NULL,
  confidence DECIMAL(4,3) DEFAULT NULL,
  decision   TINYINT NULL COMMENT '人工裁决：1通过/2驳回',
  remark     VARCHAR(500) NULL COMMENT '人工意见',
  decided_by BIGINT UNSIGNED NULL COMMENT '裁决人 sys_user.id',
  decided_at DATETIME NULL COMMENT '裁决时间',
  status     TINYINT NOT NULL DEFAULT 0 COMMENT '0待处理/1已处理',
  -- 生成列 + 唯一索引 = MySQL 版的「部分唯一索引」：
  -- 只对 status=0 的行施加「一个商品最多一条待复核」的约束，
  -- 已处理的历史行不受影响（status=1 时该列为 NULL，NULL 不参与唯一性判定）。
  --
  -- 为什么必须下沉到数据库：应用层是「先 SELECT 再 INSERT」，
  -- 两个 worker 线程同时审同一商品时，两个 SELECT 都会在对方 INSERT 之前返回空，
  -- 于是插入两行。串行测试永远抓不到，生产上会变成「同一条复核重复处理」。
  pending_item_id BIGINT UNSIGNED
    GENERATED ALWAYS AS (IF(status = 0, item_id, NULL)) VIRTUAL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_pending_item (pending_item_id),
  KEY idx_status (status),
  KEY idx_item_id (item_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='人工复核队列';

-- ------------------------------------------------------------
-- 标准答案（仅用于评测，生产环境不会有这张表）
-- ------------------------------------------------------------
CREATE TABLE audit_ground_truth (
  item_id        BIGINT UNSIGNED NOT NULL,
  expected       VARCHAR(16) NOT NULL COMMENT 'APPROVE/REJECT/REVIEW',
  violation_type VARCHAR(32) DEFAULT NULL,
  PRIMARY KEY (item_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='标注答案';

-- ------------------------------------------------------------
-- 评测结果，用于轮次对比
-- ------------------------------------------------------------
CREATE TABLE eval_runs (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  tag        VARCHAR(64) NOT NULL COMMENT 'baseline / add-rules / ...',
  metrics    JSON NOT NULL,
  started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='评测轮次';
