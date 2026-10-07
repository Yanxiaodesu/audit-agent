-- ============================================================
--  校园二手交易平台 —— 业务库 (campus_market)
--
--  取自《基于SpringBoot的校园二手交易平台》系统设计文档，
--  从原 17 张表里提取审核链路所需的 7 张核心表。
--
--  与原设计的差异（重要）：
--    item_audit 按「方案 B」改造 —— 因为 AI 审核没有管理员 ID，
--    且 audit_remark 的 255 字符装不下结构化判罚依据。
--      1. audit_user_id 改为可空
--      2. 新增 audit_source（1人工/2AI自动/3AI+人工复核）
--      3. audit_remark 由 VARCHAR(255) 扩为 TEXT
--      4. 新增 rule_hits（JSON）与 confidence
-- ============================================================

DROP DATABASE IF EXISTS campus_market;
CREATE DATABASE campus_market CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE campus_market;

-- ------------------------------------------------------------
-- 用户表
-- ------------------------------------------------------------
CREATE TABLE sys_user (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  username     VARCHAR(50)  NOT NULL,
  password     VARCHAR(100) NOT NULL DEFAULT '',
  nickname     VARCHAR(50)  NOT NULL,
  role         TINYINT      NOT NULL DEFAULT 1  COMMENT '1学生/2管理员/9系统',
  credit_score INT          NOT NULL DEFAULT 100 COMMENT '信用分，初始 100',
  status       TINYINT      NOT NULL DEFAULT 1  COMMENT '1正常/2封禁',
  deleted      TINYINT(1)   NOT NULL DEFAULT 0,
  create_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  update_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户表';

-- ------------------------------------------------------------
-- 商品分类表
-- ------------------------------------------------------------
CREATE TABLE item_category (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name        VARCHAR(50) NOT NULL,
  parent_id   BIGINT UNSIGNED NOT NULL DEFAULT 0,
  sort        INT NOT NULL DEFAULT 0,
  status      TINYINT NOT NULL DEFAULT 1,
  deleted     TINYINT(1) NOT NULL DEFAULT 0,
  create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_parent_id (parent_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品分类表';

-- ------------------------------------------------------------
-- 商品表（字段与文档一致）
-- status 枚举：0待审核/1在售/2已锁定/3已售出/4已下架/5审核驳回
-- ------------------------------------------------------------
CREATE TABLE item (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  item_no        VARCHAR(32)  NOT NULL,
  seller_id      BIGINT UNSIGNED NOT NULL,
  category_id    BIGINT UNSIGNED NOT NULL,
  title          VARCHAR(100) NOT NULL,
  description    TEXT,
  price          DECIMAL(10,2) NOT NULL,
  original_price DECIMAL(10,2) DEFAULT NULL,
  cover_img      VARCHAR(255) DEFAULT NULL,
  trade_type     TINYINT NOT NULL DEFAULT 1 COMMENT '1面交/2邮寄/3均可',
  item_condition TINYINT NOT NULL DEFAULT 2 COMMENT '1全新/2九成新/3七成新/4五成新及以下',
  status         TINYINT NOT NULL DEFAULT 0 COMMENT '0待审核/1在售/2已锁定/3已售出/4已下架/5审核驳回',
  version        INT NOT NULL DEFAULT 0 COMMENT '乐观锁版本号',
  deleted        TINYINT(1) NOT NULL DEFAULT 0,
  create_time    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  update_time    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_item_no (item_no),
  KEY idx_seller_id (seller_id),
  KEY idx_category_id (category_id),
  KEY idx_status_create_time (status, create_time),
  KEY idx_title (title),
  -- ngram 全文索引：MySQL 8 内置的 CJK 分词（按 2-gram 切分），
  -- 供 search_similar_items 做真正的文本相似检索，不需要外部 embedding 服务
  FULLTEXT KEY ft_item_text (title, description) WITH PARSER ngram
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品表';

-- ------------------------------------------------------------
-- 商品图片表
-- ------------------------------------------------------------
CREATE TABLE item_image (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  item_id     BIGINT UNSIGNED NOT NULL,
  url         VARCHAR(255) NOT NULL,
  sort        INT NOT NULL DEFAULT 0,
  deleted     TINYINT(1) NOT NULL DEFAULT 0,
  create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_item_id (item_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品图片表';

-- ------------------------------------------------------------
-- 商品审核记录表（方案 B 改造版）
-- ------------------------------------------------------------
CREATE TABLE item_audit (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  item_id       BIGINT UNSIGNED NOT NULL,
  audit_user_id BIGINT UNSIGNED DEFAULT NULL COMMENT '审核管理员ID；AI 自动审核时为空',
  audit_source  TINYINT NOT NULL DEFAULT 1 COMMENT '1人工/2AI自动/3AI+人工复核',
  audit_result  TINYINT NOT NULL COMMENT '1通过/2驳回',
  audit_remark  TEXT COMMENT '审核意见（原设计为 VARCHAR(255)，装不下结构化依据）',
  rule_hits     JSON DEFAULT NULL COMMENT '命中的规则明细',
  confidence    DECIMAL(4,3) DEFAULT NULL COMMENT '置信度 0-1',
  submit_time   DATETIME DEFAULT NULL COMMENT '商品提交审核时间',
  audit_time    DATETIME DEFAULT NULL COMMENT '审核完成时间',
  deleted       TINYINT(1) NOT NULL DEFAULT 0,
  create_time   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  update_time   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_item_id (item_id),
  KEY idx_audit_user_id (audit_user_id),
  KEY idx_audit_result (audit_result),
  KEY idx_audit_source (audit_source)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品审核记录表';

-- ------------------------------------------------------------
-- 信用分变动记录表
-- ------------------------------------------------------------
CREATE TABLE credit_record (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id      BIGINT UNSIGNED NOT NULL,
  change_score INT NOT NULL,
  before_score INT NOT NULL,
  after_score  INT NOT NULL,
  reason       VARCHAR(255) NOT NULL,
  biz_type     TINYINT NOT NULL DEFAULT 0 COMMENT '1交易完成/2差评/3被举报/4审核驳回',
  deleted      TINYINT(1) NOT NULL DEFAULT 0,
  create_time  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_user_id (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='信用分变动记录表';

-- ------------------------------------------------------------
-- 举报表
-- ------------------------------------------------------------
CREATE TABLE report (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  item_id       BIGINT UNSIGNED NOT NULL,
  reporter_id   BIGINT UNSIGNED NOT NULL,
  reason        VARCHAR(255) NOT NULL,
  status        TINYINT NOT NULL DEFAULT 0 COMMENT '0待处理/1已处理',
  handle_remark VARCHAR(255) DEFAULT NULL,
  deleted       TINYINT(1) NOT NULL DEFAULT 0,
  create_time   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_item_id (item_id),
  KEY idx_reporter_id (reporter_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='举报表';
