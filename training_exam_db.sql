-- ============================================================
-- 历年培训与考试信息数据库 - 完整建表脚本
-- 数据库: training_exam_db
-- MySQL 8.0+, utf8mb4
-- ============================================================

-- 使用数据库（容器已创建，此处确保切换）
USE training_exam_db;

-- ============================================================
-- 1. 人员信息表
-- ============================================================
CREATE TABLE IF NOT EXISTS `employee` (
    `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '人员唯一编号',
    `employee_no`     VARCHAR(50)     NOT NULL                COMMENT '工号或学号',
    `employee_name`   VARCHAR(100)    NOT NULL                COMMENT '人员姓名',
    `gender`          VARCHAR(10)     DEFAULT NULL            COMMENT '性别',
    `department_name` VARCHAR(100)    DEFAULT NULL            COMMENT '当前所属部门',
    `position_name`   VARCHAR(100)    DEFAULT NULL            COMMENT '当前职位',
    `phone`           VARCHAR(30)     DEFAULT NULL            COMMENT '联系电话',
    `email`           VARCHAR(255)    DEFAULT NULL            COMMENT '邮箱地址',
    `employee_status` VARCHAR(20)     NOT NULL DEFAULT '在职'  COMMENT '人员当前状态',
    `hire_date`       DATE            DEFAULT NULL            COMMENT '入职日期',
    `remarks`         TEXT            DEFAULT NULL            COMMENT '备注信息',
    `created_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '数据创建时间',
    `updated_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '数据最后修改时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_employee_no` (`employee_no`),
    INDEX `idx_employee_name` (`employee_name`),
    INDEX `idx_department_name` (`department_name`),
    INDEX `idx_employee_status` (`employee_status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='人员信息表';

-- ============================================================
-- 2. 培训课程表
-- ============================================================
CREATE TABLE IF NOT EXISTS `training_course` (
    `id`                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '课程唯一编号',
    `course_code`        VARCHAR(50)     NOT NULL                COMMENT '课程编号',
    `course_name`        VARCHAR(200)    NOT NULL                COMMENT '课程名称',
    `course_category`    VARCHAR(100)    DEFAULT NULL            COMMENT '课程类别',
    `standard_hours`     DECIMAL(6,2)    DEFAULT NULL            COMMENT '标准培训学时',
    `course_level`       VARCHAR(30)     DEFAULT NULL            COMMENT '课程等级',
    `course_description` TEXT            DEFAULT NULL            COMMENT '课程内容说明',
    `is_active`          TINYINT(1)      NOT NULL DEFAULT 1      COMMENT '是否启用（1=启用，0=停用）',
    `created_at`         DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '数据创建时间',
    `updated_at`         DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '数据最后修改时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_course_code` (`course_code`),
    INDEX `idx_course_name` (`course_name`),
    INDEX `idx_course_category` (`course_category`),
    CONSTRAINT `chk_standard_hours` CHECK (`standard_hours` IS NULL OR `standard_hours` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='培训课程表';

-- ============================================================
-- 3. 培训场次表
-- ============================================================
CREATE TABLE IF NOT EXISTS `training_session` (
    `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '培训场次唯一编号',
    `course_id`         BIGINT UNSIGNED NOT NULL                COMMENT '对应的培训课程编号',
    `training_year`     YEAR            NOT NULL                COMMENT '培训年度',
    `session_no`        VARCHAR(50)     NOT NULL                COMMENT '培训场次编号',
    `session_name`      VARCHAR(200)    NOT NULL                COMMENT '培训场次名称',
    `start_date`        DATE            NOT NULL                COMMENT '培训开始日期',
    `end_date`          DATE            NOT NULL                COMMENT '培训结束日期',
    `actual_hours`      DECIMAL(6,2)    DEFAULT NULL            COMMENT '实际培训学时',
    `trainer_name`      VARCHAR(100)    DEFAULT NULL            COMMENT '培训讲师姓名',
    `organizer`         VARCHAR(200)    DEFAULT NULL            COMMENT '培训组织单位',
    `training_location` VARCHAR(255)    DEFAULT NULL            COMMENT '培训地点',
    `training_method`   VARCHAR(30)     NOT NULL DEFAULT '线下'  COMMENT '培训方式',
    `session_status`    VARCHAR(30)     NOT NULL DEFAULT '计划中' COMMENT '培训状态',
    `planned_count`     INT UNSIGNED    DEFAULT NULL            COMMENT '计划参加人数',
    `participant_count` INT UNSIGNED    DEFAULT NULL            COMMENT '实际参加人数',
    `training_fee`      DECIMAL(10,2)   DEFAULT NULL            COMMENT '培训总费用',
    `remarks`           TEXT            DEFAULT NULL            COMMENT '备注信息',
    `created_at`        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '数据创建时间',
    `updated_at`        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '数据最后修改时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_session_no` (`session_no`),
    INDEX `idx_course_id` (`course_id`),
    INDEX `idx_training_year` (`training_year`),
    INDEX `idx_start_date` (`start_date`),
    INDEX `idx_session_status` (`session_status`),
    CONSTRAINT `fk_session_course` FOREIGN KEY (`course_id`) REFERENCES `training_course` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT `chk_end_date` CHECK (`end_date` >= `start_date`),
    CONSTRAINT `chk_actual_hours` CHECK (`actual_hours` IS NULL OR `actual_hours` >= 0),
    CONSTRAINT `chk_planned_count` CHECK (`planned_count` IS NULL OR `planned_count` >= 0),
    CONSTRAINT `chk_participant_count` CHECK (`participant_count` IS NULL OR `participant_count` >= 0),
    CONSTRAINT `chk_training_fee` CHECK (`training_fee` IS NULL OR `training_fee` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='培训场次表';

-- ============================================================
-- 4. 人员培训记录表
-- ============================================================
CREATE TABLE IF NOT EXISTS `training_enrollment` (
    `id`                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '培训记录唯一编号',
    `session_id`          BIGINT UNSIGNED NOT NULL                COMMENT '培训场次编号',
    `employee_id`         BIGINT UNSIGNED NOT NULL                COMMENT '参加培训的人员编号',
    `signup_date`         DATE            DEFAULT NULL            COMMENT '报名日期',
    `attendance_hours`    DECIMAL(6,2)    DEFAULT NULL            COMMENT '实际出勤学时',
    `attendance_rate`     DECIMAL(5,2)    DEFAULT NULL            COMMENT '出勤率（百分比，0.00~100.00）',
    `completion_status`   VARCHAR(30)     NOT NULL DEFAULT '已报名' COMMENT '培训完成状态',
    `completion_date`     DATE            DEFAULT NULL            COMMENT '培训完成日期',
    `evaluation_score`    DECIMAL(5,2)    DEFAULT NULL            COMMENT '培训评价分数',
    `evaluation`          VARCHAR(500)    DEFAULT NULL            COMMENT '培训评价内容',
    `certificate_no`      VARCHAR(100)    DEFAULT NULL            COMMENT '培训证书编号',
    `certificate_date`    DATE            DEFAULT NULL            COMMENT '证书发放日期',
    `department_snapshot` VARCHAR(100)    DEFAULT NULL            COMMENT '参加培训时所在部门',
    `position_snapshot`   VARCHAR(100)    DEFAULT NULL            COMMENT '参加培训时的职位',
    `remarks`             TEXT            DEFAULT NULL            COMMENT '备注信息',
    `created_at`          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '数据创建时间',
    `updated_at`          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '数据最后修改时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_session_employee` (`session_id`, `employee_id`),
    INDEX `idx_session_id` (`session_id`),
    INDEX `idx_employee_id` (`employee_id`),
    INDEX `idx_completion_status` (`completion_status`),
    INDEX `idx_department_snapshot` (`department_snapshot`),
    CONSTRAINT `fk_enrollment_session` FOREIGN KEY (`session_id`) REFERENCES `training_session` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT `fk_enrollment_employee` FOREIGN KEY (`employee_id`) REFERENCES `employee` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT `chk_attendance_rate` CHECK (`attendance_rate` IS NULL OR (`attendance_rate` >= 0 AND `attendance_rate` <= 100))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='人员培训记录表';

-- ============================================================
-- 5. 考试信息表
-- ============================================================
CREATE TABLE IF NOT EXISTS `exam` (
    `id`               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '考试唯一编号',
    `session_id`       BIGINT UNSIGNED NOT NULL                COMMENT '对应的培训场次编号',
    `exam_no`          VARCHAR(50)     NOT NULL                COMMENT '考试编号',
    `exam_name`        VARCHAR(200)    NOT NULL                COMMENT '考试名称',
    `exam_type`        VARCHAR(30)     NOT NULL DEFAULT '结业考试' COMMENT '考试类型',
    `exam_date`        DATETIME        NOT NULL                COMMENT '考试日期和时间',
    `duration_minutes` INT UNSIGNED    DEFAULT NULL            COMMENT '考试时长（分钟）',
    `total_score`      DECIMAL(6,2)    NOT NULL DEFAULT 100.00 COMMENT '考试总分',
    `pass_score`       DECIMAL(6,2)    NOT NULL DEFAULT 60.00  COMMENT '及格分数',
    `exam_method`      VARCHAR(30)     NOT NULL DEFAULT '闭卷'  COMMENT '考试方式',
    `exam_location`    VARCHAR(255)    DEFAULT NULL            COMMENT '考试地点',
    `exam_status`      VARCHAR(30)     NOT NULL DEFAULT '未开始' COMMENT '考试状态',
    `paper_count`      INT UNSIGNED    DEFAULT NULL            COMMENT '试卷数量',
    `invigilator`      VARCHAR(100)    DEFAULT NULL            COMMENT '监考人员',
    `remarks`          TEXT            DEFAULT NULL            COMMENT '备注信息',
    `created_at`       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '数据创建时间',
    `updated_at`       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '数据最后修改时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_exam_no` (`exam_no`),
    INDEX `idx_session_id` (`session_id`),
    INDEX `idx_exam_date` (`exam_date`),
    INDEX `idx_exam_type` (`exam_type`),
    INDEX `idx_exam_status` (`exam_status`),
    CONSTRAINT `fk_exam_session` FOREIGN KEY (`session_id`) REFERENCES `training_session` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT `chk_duration_minutes` CHECK (`duration_minutes` IS NULL OR `duration_minutes` >= 0),
    CONSTRAINT `chk_total_score` CHECK (`total_score` > 0),
    CONSTRAINT `chk_pass_score` CHECK (`pass_score` >= 0 AND `pass_score` <= `total_score`),
    CONSTRAINT `chk_paper_count` CHECK (`paper_count` IS NULL OR `paper_count` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='考试信息表';

-- ============================================================
-- 6. 考试成绩表
-- ============================================================
CREATE TABLE IF NOT EXISTS `exam_result` (
    `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '成绩记录唯一编号',
    `exam_id`         BIGINT UNSIGNED NOT NULL                COMMENT '考试编号',
    `employee_id`     BIGINT UNSIGNED NOT NULL                COMMENT '参加考试的人员编号',
    `attempt_no`      INT UNSIGNED    NOT NULL DEFAULT 1      COMMENT '第几次参加考试',
    `score`           DECIMAL(6,2)    DEFAULT NULL            COMMENT '考试成绩',
    `result_status`   VARCHAR(30)     NOT NULL DEFAULT '未考试' COMMENT '考试结果',
    `answer_sheet_no` VARCHAR(100)    DEFAULT NULL            COMMENT '答题卡编号',
    `paper_no`        VARCHAR(100)    DEFAULT NULL            COMMENT '试卷编号',
    `seat_no`         VARCHAR(30)     DEFAULT NULL            COMMENT '考场座位号',
    `absent_reason`   VARCHAR(500)    DEFAULT NULL            COMMENT '缺考原因',
    `graded_at`       DATETIME        DEFAULT NULL            COMMENT '成绩登记时间',
    `grader_name`     VARCHAR(100)    DEFAULT NULL            COMMENT '阅卷或成绩登记人员',
    `is_best_result`  TINYINT(1)      NOT NULL DEFAULT 0      COMMENT '是否为最佳成绩（1=是，0=否）',
    `remarks`         TEXT            DEFAULT NULL            COMMENT '备注信息',
    `created_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '数据创建时间',
    `updated_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '数据最后修改时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_exam_employee_attempt` (`exam_id`, `employee_id`, `attempt_no`),
    INDEX `idx_exam_id` (`exam_id`),
    INDEX `idx_employee_id` (`employee_id`),
    INDEX `idx_result_status` (`result_status`),
    INDEX `idx_score` (`score`),
    INDEX `idx_is_best_result` (`is_best_result`),
    CONSTRAINT `fk_result_exam` FOREIGN KEY (`exam_id`) REFERENCES `exam` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT `fk_result_employee` FOREIGN KEY (`employee_id`) REFERENCES `employee` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT `chk_attempt_no` CHECK (`attempt_no` >= 1),
    CONSTRAINT `chk_score` CHECK (`score` IS NULL OR `score` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='考试成绩表';


-- ============================================================
-- 测试数据
-- ============================================================

-- ------------------------------------------
-- 人员信息 (15人，覆盖不同部门、状态)
-- ------------------------------------------
INSERT INTO `employee` (`id`, `employee_no`, `employee_name`, `gender`, `department_name`, `position_name`, `phone`, `email`, `employee_status`, `hire_date`, `remarks`) VALUES
(1,  'EMP001', '张三',   '男', '技术部',   '软件工程师',   '13800001001', 'zhangsan@example.com',   '在职', '2018-03-15', '多次获得优秀员工'),
(2,  'EMP002', '李四',   '女', '人事部',   '人事专员',     '13800001002', 'lisi@example.com',       '在职', '2019-07-01', NULL),
(3,  'EMP003', '王五',   '男', '财务部',   '会计',         '13800001003', 'wangwu@example.com',     '离职', '2017-01-10', '2024年6月离职'),
(4,  'EMP004', '赵六',   '女', '技术部',   '高级软件工程师', '13800001004', 'zhaoliu@example.com',    '在职', '2016-05-20', '技术骨干'),
(5,  'EMP005', '孙七',   '男', '管理部',   '部门经理',     '13800001005', 'sunqi@example.com',      '在职', '2015-09-01', NULL),
(6,  'EMP006', '周八',   '女', '安全部',   '安全主管',     '13800001006', 'zhouba@example.com',     '在职', '2020-02-15', NULL),
(7,  'EMP007', '吴九',   '男', '技术部',   '软件工程师',   '13800001007', 'wujiu@example.com',      '在职', '2021-08-01', '新人'),
(8,  'EMP008', '郑十',   '女', '财务部',   '出纳',         '13800001008', 'zhengshi@example.com',   '在职', '2019-11-20', NULL),
(9,  'EMP009', '钱十一', '男', '技术部',   '架构师',       '13800001009', 'qianshiyi@example.com',  '在职', '2014-03-01', NULL),
(10, 'EMP010', '陈十二', '女', '人事部',   '人事经理',     '13800001010', 'chenshier@example.com',  '在职', '2017-06-15', NULL),
(11, 'EMP011', '刘十三', '男', '技术部',   '实习生',       '13800001011', 'liushisan@example.com',  '实习', '2025-03-01', '2025届实习生'),
(12, 'EMP012', '黄十四', '女', '安全部',   '安全专员',     '13800001012', 'huangshisi@example.com', '在职', '2022-04-10', NULL),
(13, 'EMP013', '林十五', '男', '管理部',   '副总经理',     '13800001013', 'linshiwu@example.com',   '在职', '2013-01-05', '高管'),
(14, 'EMP014', '杨十六', '女', '财务部',   '财务主管',     '13800001014', 'yangshiliu@example.com', '退休', '2010-08-20', '2025年1月退休'),
(15, 'EMP015', '何十七', '男', '技术部',   '测试工程师',   '13800001015', 'heshiqi@example.com',    '在职', '2023-02-28', NULL);

-- ------------------------------------------
-- 培训课程 (8门)
-- ------------------------------------------
INSERT INTO `training_course` (`id`, `course_code`, `course_name`, `course_category`, `standard_hours`, `course_level`, `course_description`, `is_active`) VALUES
(1, 'SAFE-001', '安全生产基础培训',   '安全培训', 8.00,  '初级', '涵盖安全生产法律法规、安全操作规程、事故预防与应急处理等基础内容。', 1),
(2, 'NET-001',  '网络安全意识培训',   '信息安全', 6.00,  '通用', '介绍网络安全基本概念、常见网络攻击手段、数据保护措施和安全行为规范。', 1),
(3, 'FIRE-001', '消防安全培训',       '安全培训', 4.00,  '通用', '包括火灾预防知识、灭火器使用方法、逃生自救技能等消防安全内容。', 1),
(4, 'MGMT-001', '中层管理技能提升',   '管理培训', 16.00, '中级', '针对中层管理者的领导力、沟通技巧、团队建设和绩效管理培训。', 1),
(5, 'TECH-001', 'Python数据分析进阶', '技术培训', 24.00, '高级', '深入学习Python在数据分析、可视化和机器学习中的应用。', 1),
(6, 'LAW-001',  '劳动法律法规培训',   '法律培训', 8.00,  '通用', '讲解劳动法、劳动合同法、社保法规等与企业和员工密切相关的法律知识。', 1),
(7, 'QUAL-001', '质量管理体系内审员', '质量培训', 12.00, '中级', 'ISO 9001质量管理体系内部审核员培训，包括标准解读和审核技巧。', 1),
(8, 'FIRST-001','急救知识与技能培训', '健康培训', 4.00,  '通用', '心肺复苏(CPR)、AED使用、创伤急救等基本急救技能培训。', 1);

-- ------------------------------------------
-- 培训场次 (12场，覆盖2023-2025年)
-- ------------------------------------------
INSERT INTO `training_session` (`id`, `course_id`, `training_year`, `session_no`, `session_name`, `start_date`, `end_date`, `actual_hours`, `trainer_name`, `organizer`, `training_location`, `training_method`, `session_status`, `planned_count`, `participant_count`, `training_fee`, `remarks`) VALUES
(1,  1, 2025, 'TS-2025-001', '2025年第一期安全生产基础培训',       '2025-05-10', '2025-05-11', 8.00,  '李安全', '安全管理部', '公司A栋301会议室',     '线下',         '已结束', 30, 28, 5000.00,  '年度例行安全培训'),
(2,  2, 2024, 'TS-2024-001', '2024年网络安全意识培训',             '2024-09-15', '2024-09-15', 6.00,  '王网络', '信息技术部', '公司B栋201培训室',     '线下',         '已结束', 50, 45, 3000.00,  NULL),
(3,  3, 2023, 'TS-2023-001', '2023年消防安全培训',                 '2023-06-20', '2023-06-20', 4.00,  '刘消防', '安全管理部', '公司A栋大厅',          '线下',         '已结束', 40, 38, 2000.00,  '含实操演练'),
(4,  4, 2025, 'TS-2025-002', '2025年第一期中层管理技能提升培训',   '2025-03-01', '2025-03-04', 16.00, '张管理', '人力资源部', '外部培训中心',         '线下',         '已结束', 20, 18, 15000.00, '外聘讲师'),
(5,  5, 2025, 'TS-2025-003', '2025年Python数据分析进阶培训',      '2025-06-01', '2025-06-05', 24.00, '陈数据', '技术部',     '公司B栋301机房',       '线上',         '进行中', 15, 12, 8000.00,  '线上直播+录播'),
(6,  1, 2024, 'TS-2024-002', '2024年第一期安全生产基础培训',       '2024-05-08', '2024-05-09', 8.00,  '李安全', '安全管理部', '公司A栋301会议室',     '线下',         '已结束', 30, 30, 5000.00,  '年度例行安全培训'),
(7,  2, 2025, 'TS-2025-004', '2025年网络安全意识培训',             '2025-09-01', '2025-09-01', 6.00,  '王网络', '信息技术部', '公司B栋201培训室',     '线上线下结合', '报名中', 50, NULL, 4000.00, '新增线上参会方式'),
(8,  3, 2024, 'TS-2024-003', '2024年消防安全培训',                 '2024-06-15', '2024-06-15', 4.00,  '刘消防', '安全管理部', '公司A栋大厅',          '线下',         '已结束', 40, 35, 2000.00, NULL),
(9,  6, 2025, 'TS-2025-005', '2025年劳动法律法规培训',             '2025-04-10', '2025-04-11', 8.00,  '赵法律', '人力资源部', '公司A栋301会议室',     '线下',         '已结束', 40, 36, 6000.00, NULL),
(10, 7, 2025, 'TS-2025-006', '2025年质量管理体系内审员培训',       '2025-07-15', '2025-07-17', 12.00, '周质量', '质量管理部', '外部培训中心',         '线下',         '计划中', 25, NULL, 12000.00, NULL),
(11, 8, 2024, 'TS-2024-004', '2024年急救知识与技能培训',           '2024-11-10', '2024-11-10', 4.00,  '孙急救', '人力资源部', '公司A栋大厅',          '线下',         '已结束', 30, 28, 1500.00, '红十字会合作培训'),
(12, 1, 2023, 'TS-2023-002', '2023年第一期安全生产基础培训',       '2023-05-06', '2023-05-07', 8.00,  '李安全', '安全管理部', '公司A栋301会议室',     '线下',         '已取消', 30, NULL, NULL, '因疫情取消');

-- ------------------------------------------
-- 人员培训记录 (38条)
-- ------------------------------------------
INSERT INTO `training_enrollment` (`id`, `session_id`, `employee_id`, `signup_date`, `attendance_hours`, `attendance_rate`, `completion_status`, `completion_date`, `evaluation_score`, `evaluation`, `certificate_no`, `certificate_date`, `department_snapshot`, `position_snapshot`, `remarks`) VALUES
-- TS-2025-001: 2025年安全生产基础培训 (session_id=1)
(1,  1,  1,  '2025-04-20', 8.00,  100.00, '已完成', '2025-05-11', 95.00, '内容实用，讲师专业',          'CERT-2025-001', '2025-05-20', '技术部', '软件工程师',    NULL),
(2,  1,  2,  '2025-04-21', 6.00,  75.00,  '已完成', '2025-05-11', 80.00, '部分内容未能参加',            'CERT-2025-002', '2025-05-20', '人事部', '人事专员',      '有半天请假'),
(3,  1,  4,  '2025-04-20', 8.00,  100.00, '已完成', '2025-05-11', 92.00, '讲师互动很好',                 'CERT-2025-003', '2025-05-20', '技术部', '高级软件工程师', NULL),
(4,  1,  5,  '2025-04-22', 8.00,  100.00, '已完成', '2025-05-11', 88.00, NULL,                          'CERT-2025-004', '2025-05-20', '管理部', '部门经理',      NULL),
(5,  1,  7,  '2025-04-20', 4.00,  50.00,  '未完成', NULL,         NULL,   NULL,                          NULL,            NULL,          '技术部', '软件工程师',    '中途因紧急任务离场'),

-- TS-2024-001: 2024年网络安全意识培训 (session_id=2)
(6,  2,  1,  '2024-08-25', 6.00,  100.00, '已完成', '2024-09-15', 90.00, '网络安全很重要',              'CERT-2024-001', '2024-09-25', '技术部', '软件工程师',    NULL),
(7,  2,  3,  '2024-08-26', 6.00,  100.00, '已完成', '2024-09-15', 85.00, NULL,                          'CERT-2024-002', '2024-09-25', '财务部', '会计',          '离职前最后一次培训'),
(8,  2,  6,  '2024-08-25', 6.00,  100.00, '已完成', '2024-09-15', 98.00, '非常实用的培训',              'CERT-2024-003', '2024-09-25', '安全部', '安全主管',      NULL),
(9,  2,  9,  '2024-08-28', 6.00,  100.00, '已完成', '2024-09-15', 88.00, NULL,                          'CERT-2024-004', '2024-09-25', '技术部', '架构师',        NULL),
(10, 2,  12, '2024-08-26', 0.00,  0.00,   '缺席',   NULL,         NULL,   NULL,                          NULL,            NULL,          '安全部', '安全专员',      '因出差缺席'),

-- TS-2023-001: 2023年消防安全培训 (session_id=3)
(11, 3,  1,  '2023-06-01', 4.00,  100.00, '已完成', '2023-06-20', 92.00, NULL,                          'CERT-2023-001', '2023-07-01', '技术部', '软件工程师',    NULL),
(12, 3,  2,  '2023-06-02', 4.00,  100.00, '已完成', '2023-06-20', 90.00, '实操很有意义',                'CERT-2023-002', '2023-07-01', '人事部', '人事专员',      NULL),
(13, 3,  4,  '2023-06-01', 4.00,  100.00, '已完成', '2023-06-20', 94.00, NULL,                          'CERT-2023-003', '2023-07-01', '技术部', '高级软件工程师', NULL),

-- TS-2025-002: 2025年管理技能培训 (session_id=4)
(14, 4,  5,  '2025-02-15', 16.00, 100.00, '已完成', '2025-03-04', 91.00, '管理理念有很大收获',          'CERT-2025-005', '2025-03-15', '管理部', '部门经理',      NULL),
(15, 4,  6,  '2025-02-15', 16.00, 100.00, '已完成', '2025-03-04', 87.00, '案例讨论很有启发',            'CERT-2025-006', '2025-03-15', '安全部', '安全主管',      NULL),
(16, 4,  13, '2025-02-16', 12.00, 75.00,  '已完成', '2025-03-04', 82.00, '部分课程因会议请假',          'CERT-2025-007', '2025-03-15', '管理部', '副总经理',      '有半天因会议缺席'),

-- TS-2025-003: 2025年Python培训 (session_id=5, 进行中)
(17, 5,  1,  '2025-05-15', 12.00, NULL,    '培训中', NULL,         NULL,   NULL,                          NULL,            NULL,          '技术部', '软件工程师',    NULL),
(18, 5,  7,  '2025-05-15', 16.00, NULL,    '培训中', NULL,         NULL,   NULL,                          NULL,            NULL,          '技术部', '软件工程师',    NULL),
(19, 5,  9,  '2025-05-16', 20.00, NULL,    '培训中', NULL,         NULL,   NULL,                          NULL,            NULL,          '技术部', '架构师',        NULL),
(20, 5,  15, '2025-05-15', 8.00,  NULL,    '培训中', NULL,         NULL,   NULL,                          NULL,            NULL,          '技术部', '测试工程师',    NULL),

-- TS-2024-002: 2024年安全生产培训 (session_id=6)
(21, 6,  1,  '2024-04-15', 8.00,  100.00, '已完成', '2024-05-09', 93.00, NULL,                          'CERT-2024-005', '2024-05-20', '技术部', '软件工程师',    NULL),
(22, 6,  2,  '2024-04-16', 8.00,  100.00, '已完成', '2024-05-09', 88.00, NULL,                          'CERT-2024-006', '2024-05-20', '人事部', '人事专员',      NULL),
(23, 6,  4,  '2024-04-15', 8.00,  100.00, '已完成', '2024-05-09', 90.00, NULL,                          'CERT-2024-007', '2024-05-20', '技术部', '高级软件工程师', NULL),
(24, 6,  5,  '2024-04-18', 6.00,  75.00,  '已完成', '2024-05-09', 75.00, NULL,                          'CERT-2024-008', '2024-05-20', '管理部', '部门经理',      NULL),

-- TS-2024-003: 2024年消防安全培训 (session_id=8)
(25, 8,  1,  '2024-05-25', 4.00,  100.00, '已完成', '2024-06-15', 95.00, NULL,                          'CERT-2024-009', '2024-06-25', '技术部', '软件工程师',    NULL),
(26, 8,  7,  '2024-05-26', 4.00,  100.00, '已完成', '2024-06-15', 88.00, NULL,                          'CERT-2024-010', '2024-06-25', '技术部', '软件工程师',    NULL),
(27, 8,  12, '2024-05-25', 4.00,  100.00, '已完成', '2024-06-15', 90.00, NULL,                          'CERT-2024-011', '2024-06-25', '安全部', '安全专员',      NULL),

-- TS-2025-005: 2025年劳动法律法规培训 (session_id=9)
(28, 9,  2,  '2025-03-20', 8.00,  100.00, '已完成', '2025-04-11', 85.00, NULL,                          'CERT-2025-008', '2025-04-20', '人事部', '人事专员',      NULL),
(29, 9,  5,  '2025-03-21', 8.00,  100.00, '已完成', '2025-04-11', 82.00, '法律知识很有用',              'CERT-2025-009', '2025-04-20', '管理部', '部门经理',      NULL),
(30, 9,  8,  '2025-03-20', 8.00,  100.00, '已完成', '2025-04-11', 90.00, NULL,                          'CERT-2025-010', '2025-04-20', '财务部', '出纳',          NULL),
(31, 9,  10, '2025-03-20', 4.00,  50.00,  '未完成', NULL,         NULL,   NULL,                          NULL,            NULL,          '人事部', '人事经理',      '有半天请假'),

-- TS-2024-004: 2024年急救培训 (session_id=11)
(32, 11, 1,  '2024-10-20', 4.00,  100.00, '已完成', '2024-11-10', 96.00, '很实用的急救知识',            'CERT-2024-012', '2024-11-20', '技术部', '软件工程师',    NULL),
(33, 11, 6,  '2024-10-20', 4.00,  100.00, '已完成', '2024-11-10', 92.00, NULL,                          'CERT-2024-013', '2024-11-20', '安全部', '安全主管',      NULL),
(34, 11, 8,  '2024-10-21', 4.00,  100.00, '已完成', '2024-11-10', 88.00, NULL,                          'CERT-2024-014', '2024-11-20', '财务部', '出纳',          NULL),

-- TS-2023-002: 2023年安全生产培训-已取消 (session_id=12)
(35, 12, 1,  '2023-04-20', NULL,  NULL,    '已取消', NULL,         NULL,   NULL,                          NULL,            NULL,          '技术部', '软件工程师',    '培训因疫情取消'),
(36, 12, 2,  '2023-04-21', NULL,  NULL,    '已取消', NULL,         NULL,   NULL,                          NULL,            NULL,          '人事部', '人事专员',      '培训因疫情取消'),

-- 历史快照测试: 王五(EMP003)在2023年还在财务部为"会计"，2024年离职前也参加过培训
(37, 3,  3,  '2023-06-01', 4.00,  100.00, '已完成', '2023-06-20', 87.00, NULL,                          'CERT-2023-004', '2023-07-01', '财务部', '会计',          NULL),
-- 张三2023年在技术部-软件工程师，已经参加多次培训（演示历史快照意义）
(38, 6,  11, '2024-04-15', NULL,  NULL,    '已报名', NULL,         NULL,   NULL,                          NULL,            NULL,          '技术部', '实习生',        '实习期间报名但未参加');

-- ------------------------------------------
-- 考试信息 (8场考试)
-- ------------------------------------------
INSERT INTO `exam` (`id`, `session_id`, `exam_no`, `exam_name`, `exam_type`, `exam_date`, `duration_minutes`, `total_score`, `pass_score`, `exam_method`, `exam_location`, `exam_status`, `paper_count`, `invigilator`, `remarks`) VALUES
(1,  1, 'EX-2025-001', '2025年安全生产培训结业考试', '结业考试', '2025-05-11 14:00:00', 90,  100.00, 60.00, '闭卷', '公司A栋301会议室', '已结束', 30, '李安全, 王监考', NULL),
(2,  1, 'EX-2025-002', '2025年安全生产培训补考',    '补考',    '2025-05-25 14:00:00', 90,  100.00, 60.00, '闭卷', '公司A栋302会议室', '已结束', 5,  '李安全',          '第一次补考'),
(3,  2, 'EX-2024-001', '2024年网络安全培训考试',    '结业考试', '2024-09-15 15:00:00', 60,  100.00, 60.00, '机考', '公司B栋301机房',   '已结束', 50, '王网络, 赵监考',  NULL),
(4,  4, 'EX-2025-003', '2025年管理技能培训结业考试', '结业考试', '2025-03-04 13:00:00', 120, 100.00, 70.00, '开卷', '公司A栋301会议室', '已结束', 20, '张管理',          '管理类考试及格线为70分'),
(5,  5, 'EX-2025-004', '2025年Python培训阶段考试',  '阶段考试', '2025-06-03 10:00:00', 120, 100.00, 60.00, '机考', '公司B栋301机房',   '已结束', 15, '陈数据',          NULL),
(6,  3, 'EX-2023-001', '2023年消防安全培训考试',    '结业考试', '2023-06-20 15:00:00', 45,  80.00,  48.00, '闭卷', '公司A栋大厅',      '已结束', 40, '刘消防',          '消防安全考试总分为80分'),
(7,  9, 'EX-2025-005', '2025年劳动法培训结业考试',  '结业考试', '2025-04-11 14:00:00', 90,  100.00, 60.00, '闭卷', '公司A栋301会议室', '已结束', 40, '赵法律, 王监考',  NULL),
(8,  10,'EX-2025-006', '2025年质量体系内审员认证',  '认证考试', '2025-07-17 14:00:00', 150, 120.00, 84.00, '闭卷', '外部培训中心',      '未开始', 25, '周质量',          '认证考试总分120分，及格线84分(70%)');

-- ------------------------------------------
-- 考试成绩 (26条)
-- ------------------------------------------
INSERT INTO `exam_result` (`id`, `exam_id`, `employee_id`, `attempt_no`, `score`, `result_status`, `answer_sheet_no`, `paper_no`, `seat_no`, `absent_reason`, `graded_at`, `grader_name`, `is_best_result`, `remarks`) VALUES
-- EX-2025-001: 安全生产结业考试 (exam_id=1)
(1,  1, 1,  1, 86.00, '合格',   'AS-2025-001', 'PAPER-A-001', 'A01', NULL,    '2025-05-12 10:00:00', '李安全', 1, NULL),
(2,  1, 2,  1, 55.00, '不合格', 'AS-2025-002', 'PAPER-A-002', 'A02', NULL,    '2025-05-12 10:00:00', '李安全', 0, '需参加补考'),
(3,  1, 4,  1, 92.00, '合格',   'AS-2025-003', 'PAPER-A-003', 'A03', NULL,    '2025-05-12 10:00:00', '李安全', 1, NULL),
(4,  1, 5,  1, 78.00, '合格',   'AS-2025-004', 'PAPER-A-004', 'A04', NULL,    '2025-05-12 10:00:00', '李安全', 1, NULL),
(5,  1, 7,  1, 72.00, '合格',   'AS-2025-005', 'PAPER-A-005', 'A05', NULL,    '2025-05-12 10:00:00', '李安全', 1, NULL),

-- EX-2025-002: 安全生产补考 (exam_id=2) - 李四第2次参加
(6,  2, 2,  2, 78.00, '合格',   'AS-2025-006', 'PAPER-B-001', 'B01', NULL,    '2025-05-26 09:00:00', '李安全', 1, '补考通过'),

-- EX-2024-001: 网络安全考试 (exam_id=3)
(7,  3, 1,  1, 88.00, '合格',   'AS-2024-001', 'PAPER-C-001', 'C01', NULL,    '2024-09-16 14:00:00', '王网络', 1, NULL),
(8,  3, 3,  1, 76.00, '合格',   'AS-2024-002', 'PAPER-C-002', 'C02', NULL,    '2024-09-16 14:00:00', '王网络', 1, NULL),
(9,  3, 6,  1, 96.00, '合格',   'AS-2024-003', 'PAPER-C-003', 'C03', NULL,    '2024-09-16 14:00:00', '王网络', 1, NULL),
(10, 3, 9,  1, 82.00, '合格',   'AS-2024-004', 'PAPER-C-004', 'C04', NULL,    '2024-09-16 14:00:00', '王网络', 1, NULL),
(11, 3, 12, 1, NULL,  '缺考',   NULL,           NULL,           'C05', '出差未归','2024-09-16 14:00:00', '王网络', 0, '因出差未能参加考试'),

-- EX-2025-003: 管理技能考试 (exam_id=4, 及格线70)
(12, 4, 5,  1, 84.00, '合格',   'AS-2025-007', 'PAPER-D-001', 'D01', NULL,    '2025-03-05 16:00:00', '张管理', 1, NULL),
(13, 4, 6,  1, 78.00, '合格',   'AS-2025-008', 'PAPER-D-002', 'D02', NULL,    '2025-03-05 16:00:00', '张管理', 1, NULL),
(14, 4, 13, 1, 65.00, '不合格', 'AS-2025-009', 'PAPER-D-003', 'D03', NULL,    '2025-03-05 16:00:00', '张管理', 0, '低于及格线70分'),

-- EX-2025-004: Python阶段考试 (exam_id=5)
(15, 5, 1,  1, 90.00, '合格',   'AS-2025-010', 'PAPER-E-001', 'E01', NULL,    '2025-06-04 11:00:00', '陈数据', 1, NULL),
(16, 5, 7,  1, 72.00, '合格',   'AS-2025-011', 'PAPER-E-002', 'E02', NULL,    '2025-06-04 11:00:00', '陈数据', 1, NULL),
(17, 5, 9,  1, 95.00, '合格',   'AS-2025-012', 'PAPER-E-003', 'E03', NULL,    '2025-06-04 11:00:00', '陈数据', 1, NULL),
(18, 5, 15, 1, 58.00, '不合格', 'AS-2025-013', 'PAPER-E-004', 'E04', NULL,    '2025-06-04 11:00:00', '陈数据', 0, NULL),

-- EX-2023-001: 消防安全考试 (exam_id=6, 总分80, 及格48)
(19, 6, 1,  1, 72.00, '合格',   'AS-2023-001', 'PAPER-F-001', 'F01', NULL,    '2023-06-21 09:00:00', '刘消防', 1, NULL),
(20, 6, 2,  1, 68.00, '合格',   'AS-2023-002', 'PAPER-F-002', 'F02', NULL,    '2023-06-21 09:00:00', '刘消防', 1, NULL),
(21, 6, 4,  1, 70.00, '合格',   'AS-2023-003', 'PAPER-F-003', 'F03', NULL,    '2023-06-21 09:00:00', '刘消防', 1, NULL),

-- EX-2025-005: 劳动法考试 (exam_id=7)
(22, 7, 2,  1, 82.00, '合格',   'AS-2025-014', 'PAPER-G-001', 'G01', NULL,    '2025-04-12 15:00:00', '赵法律', 1, NULL),
(23, 7, 5,  1, 76.00, '合格',   'AS-2025-015', 'PAPER-G-002', 'G02', NULL,    '2025-04-12 15:00:00', '赵法律', 1, NULL),
(24, 7, 8,  1, 88.00, '合格',   'AS-2025-016', 'PAPER-G-003', 'G03', NULL,    '2025-04-12 15:00:00', '赵法律', 1, NULL),
(25, 7, 10, 1, 45.00, '不合格', 'AS-2025-017', 'PAPER-G-004', 'G04', NULL,    '2025-04-12 15:00:00', '赵法律', 0, '未达到及格线'),

-- 作弊测试用例: 刘十三(EMP011, id=11) 考试作弊
(26, 7, 11, 1, 92.00, '作弊',   'AS-2025-018', 'PAPER-G-005', 'G05', NULL,    '2025-04-12 15:00:00', '赵法律', 0, '考试中携带禁止资料，成绩作废');

-- 多attempt测试: EMP001 张三在EX-2025-001中第1次86分，如果再参加一次补考
INSERT INTO `exam_result` (`id`, `exam_id`, `employee_id`, `attempt_no`, `score`, `result_status`, `answer_sheet_no`, `paper_no`, `seat_no`, `absent_reason`, `graded_at`, `grader_name`, `is_best_result`, `remarks`) VALUES
(27, 2, 1,  2, 94.00, '合格',   'AS-2025-020', 'PAPER-B-003', 'B03', NULL,    '2025-05-26 09:00:00', '李安全', 1, '张三自愿参加补考提高成绩');

-- 更新张三在原考试中的 is_best_result
UPDATE `exam_result` SET `is_best_result` = 0 WHERE `id` = 1;
