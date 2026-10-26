-- 初始数据库结构。每个字段均带中文 COMMENT，便于数据库客户端直接查看。
CREATE TABLE IF NOT EXISTS users (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '用户编号',
    username VARCHAR(40) NOT NULL COMMENT '登录用户名',
    display_name VARCHAR(120) NOT NULL COMMENT '用户显示姓名',
    password_salt CHAR(32) NOT NULL COMMENT '密码哈希盐值',
    password_hash CHAR(64) NOT NULL COMMENT 'PBKDF2 密码哈希',
    role VARCHAR(16) NOT NULL COMMENT '用户角色：admin 管理员，student 学生',
    enabled TINYINT(1) NOT NULL DEFAULT 1 COMMENT '账号启用状态：1 启用，0 停用',
    created_at DOUBLE NOT NULL COMMENT '账号创建时间的 Unix 时间戳',
    PRIMARY KEY (id),
    UNIQUE KEY users_username_uq (username),
    CONSTRAINT users_role_ck CHECK (role IN ('admin','student'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='平台管理员与学生账号';

CREATE TABLE IF NOT EXISTS sessions (
    token_hash CHAR(64) NOT NULL COMMENT '登录令牌的 SHA-256 哈希',
    csrf_token VARCHAR(128) NOT NULL COMMENT '跨站请求伪造校验令牌',
    user_id BIGINT UNSIGNED NOT NULL COMMENT '所属用户编号',
    expires_at DOUBLE NOT NULL COMMENT '会话过期时间的 Unix 时间戳',
    PRIMARY KEY (token_hash),
    KEY sessions_expiry_idx (expires_at),
    CONSTRAINT sessions_user_fk FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户登录会话';

CREATE TABLE IF NOT EXISTS questions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '题目编号',
    section VARCHAR(20) NOT NULL COMMENT '题型：single、multiple、true_false 或 fill_blank',
    number INT NOT NULL COMMENT '该题型内按 PDF 编排的题号',
    source_page INT NOT NULL DEFAULT 0 COMMENT '题目来源 PDF 页码',
    stem MEDIUMTEXT NOT NULL COMMENT '题目正文',
    options_json MEDIUMTEXT NOT NULL COMMENT '选择题选项的 JSON 文本',
    answer MEDIUMTEXT NOT NULL COMMENT '标准答案文本',
    explanation MEDIUMTEXT NOT NULL COMMENT '题目解析',
    subject VARCHAR(120) NOT NULL DEFAULT '' COMMENT '所属科目或章节',
    difficulty VARCHAR(40) NOT NULL DEFAULT '' COMMENT '题目难度标记',
    active TINYINT(1) NOT NULL DEFAULT 1 COMMENT '题目启用状态：1 启用，0 停用',
    created_at DOUBLE NOT NULL COMMENT '题目创建时间的 Unix 时间戳',
    updated_at DOUBLE NOT NULL COMMENT '题目最近修改时间的 Unix 时间戳',
    PRIMARY KEY (id),
    UNIQUE KEY questions_section_number_uq (section, number),
    KEY questions_section_idx (section, active, number),
    CONSTRAINT questions_section_ck CHECK (section IN ('single','multiple','true_false','fill_blank'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='按 PDF 题型和题号顺序维护的题库';

CREATE TABLE IF NOT EXISTS practice_progress (
    user_id BIGINT UNSIGNED NOT NULL COMMENT '学生用户编号',
    question_id BIGINT UNSIGNED NOT NULL COMMENT '题目编号',
    attempts INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '累计作答次数',
    correct_count INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '累计答对次数',
    incorrect_count INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '累计答错次数',
    last_answer MEDIUMTEXT NOT NULL COMMENT '最近一次提交的答案',
    last_result TINYINT(1) NOT NULL DEFAULT 0 COMMENT '最近一次作答结果：1 正确，0 错误',
    last_seen_at DOUBLE NOT NULL COMMENT '最近作答时间的 Unix 时间戳',
    PRIMARY KEY (user_id, question_id),
    CONSTRAINT practice_progress_user_fk FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT practice_progress_question_fk FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='学生逐题练习进度和答题统计';

CREATE TABLE IF NOT EXISTS favorites (
    user_id BIGINT UNSIGNED NOT NULL COMMENT '收藏该题的学生用户编号',
    question_id BIGINT UNSIGNED NOT NULL COMMENT '被收藏的题目编号',
    created_at DOUBLE NOT NULL COMMENT '收藏创建时间的 Unix 时间戳',
    PRIMARY KEY (user_id, question_id),
    CONSTRAINT favorites_user_fk FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT favorites_question_fk FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='学生个人题目收藏';

CREATE TABLE IF NOT EXISTS exam_attempts (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '模拟考试记录编号',
    user_id BIGINT UNSIGNED NOT NULL COMMENT '参加考试的学生用户编号',
    started_at DOUBLE NOT NULL COMMENT '考试开始时间的 Unix 时间戳',
    ends_at DOUBLE NOT NULL COMMENT '考试截止时间的 Unix 时间戳',
    submitted_at DOUBLE NULL COMMENT '实际交卷时间的 Unix 时间戳',
    status VARCHAR(20) NOT NULL DEFAULT 'in_progress' COMMENT '考试状态：in_progress 作答中，submitted 已交卷',
    config_json MEDIUMTEXT NOT NULL COMMENT '开考时保存的考试配置快照',
    score DOUBLE NULL COMMENT '本场考试得分',
    max_score DOUBLE NULL COMMENT '本场考试满分',
    PRIMARY KEY (id),
    KEY exam_attempt_user_idx (user_id, started_at),
    CONSTRAINT exam_attempts_user_fk FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT exam_attempts_status_ck CHECK (status IN ('in_progress','submitted'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='学生模拟考试记录';

CREATE TABLE IF NOT EXISTS exam_items (
    exam_id BIGINT UNSIGNED NOT NULL COMMENT '所属模拟考试编号',
    order_index INT UNSIGNED NOT NULL COMMENT '该题在本场试卷中的顺序',
    question_id BIGINT UNSIGNED NOT NULL COMMENT '题目编号',
    snapshot_json MEDIUMTEXT NOT NULL COMMENT '开考时题目内容及答案快照',
    answer_json MEDIUMTEXT NULL COMMENT '学生本题作答内容的 JSON 文本',
    PRIMARY KEY (exam_id, question_id),
    UNIQUE KEY exam_items_order_uq (exam_id, order_index),
    CONSTRAINT exam_items_exam_fk FOREIGN KEY (exam_id) REFERENCES exam_attempts(id) ON DELETE CASCADE,
    CONSTRAINT exam_items_question_fk FOREIGN KEY (question_id) REFERENCES questions(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='模拟考试试卷题目和学生作答';

CREATE TABLE IF NOT EXISTS settings (
    `key` VARCHAR(100) NOT NULL COMMENT '配置项名称',
    value_json MEDIUMTEXT NOT NULL COMMENT '配置项 JSON 内容',
    PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='平台级配置';

CREATE TABLE IF NOT EXISTS admin_audit (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '操作记录编号',
    admin_id BIGINT UNSIGNED NOT NULL COMMENT '执行操作的管理员编号',
    action VARCHAR(100) NOT NULL COMMENT '操作类型',
    details VARCHAR(500) NOT NULL COMMENT '操作详情摘要',
    created_at DOUBLE NOT NULL COMMENT '操作时间的 Unix 时间戳',
    PRIMARY KEY (id),
    KEY admin_audit_created_idx (created_at),
    CONSTRAINT admin_audit_admin_fk FOREIGN KEY (admin_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='管理员关键操作审计记录';

CREATE TABLE IF NOT EXISTS import_jobs (
    id VARCHAR(64) NOT NULL COMMENT 'PDF 导入预览任务编号',
    admin_id BIGINT UNSIGNED NOT NULL COMMENT '创建导入任务的管理员编号',
    source_name VARCHAR(255) NOT NULL COMMENT '上传的 PDF 文件名',
    source_sha256 CHAR(64) NOT NULL COMMENT '上传 PDF 文件的 SHA-256 校验值',
    questions_json LONGTEXT NOT NULL COMMENT '待确认导入的完整题目 JSON 数据',
    counts_json MEDIUMTEXT NOT NULL COMMENT '各题型题量的 JSON 数据',
    created_at DOUBLE NOT NULL COMMENT '预览任务创建时间的 Unix 时间戳',
    committed_at DOUBLE NULL COMMENT '确认导入时间的 Unix 时间戳',
    PRIMARY KEY (id),
    KEY import_jobs_admin_created_idx (admin_id, created_at),
    CONSTRAINT import_jobs_admin_fk FOREIGN KEY (admin_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='PDF 题库导入预览与确认任务';
