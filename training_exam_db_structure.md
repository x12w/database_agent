# 历年培训与考试信息数据库 — 完整结构文档

## 1. 数据库基本信息

| 项目         | 值                              |
| ------------ | ------------------------------- |
| 数据库名称   | `training_exam_db`              |
| 数据库用途   | 保存历年培训课程、培训场次、参加人员、考试安排和考试成绩等信息 |
| 数据库类型   | MySQL 8.0                       |
| 字符集       | `utf8mb4`                       |
| 排序规则     | `utf8mb4_0900_ai_ci`            |
| 存储引擎     | InnoDB                          |
| 数据表数量   | 6                               |
| 外键数量     | 6                               |
| CHECK 约束   | 14                              |
| 唯一约束     | 6                               |
| 索引数量     | 24（含主键和唯一键）              |

### 1.1 连接信息

| 项目     | 值                   |
| -------- | -------------------- |
| 容器名   | `mysql-training`     |
| 宿主机端口 | `3307`              |
| 容器端口   | `3306`              |
| 用户名   | `root`               |
| 密码     | `training123`         |

```bash
# 容器内连接
docker exec -it mysql-training mysql -uroot -ptraining123 training_exam_db

# 宿主机连接
mysql -h 127.0.0.1 -P 3307 -uroot -ptraining123 training_exam_db
```

### 1.2 建库语句

```sql
CREATE DATABASE training_exam_db
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;
```

---

## 2. 表结构概览

| 序号 | 表名                    | 中文名称       | 主要用途               | 数据量 |
| ---: | ----------------------- | -------------- | ---------------------- | -----: |
|    1 | `employee`              | 人员信息表     | 保存参加培训和考试的人员信息 |     15 |
|    2 | `training_course`       | 培训课程表     | 保存培训课程的基础信息       |      8 |
|    3 | `training_session`      | 培训场次表     | 保存每年实际举办的培训活动   |     12 |
|    4 | `training_enrollment`   | 人员培训记录表 | 保存人员参加培训的情况       |     38 |
|    5 | `exam`                  | 考试信息表     | 保存培训对应的考试安排       |      8 |
|    6 | `exam_result`           | 考试成绩表     | 保存人员的考试成绩和结果     |     27 |

---

## 3. 实体关系图

```text
training_course (培训课程)
    │
    │ 1 : N  (一门课程可举办多个场次)
    ▼
training_session (培训场次)
    │                           │
    │ 1 : N                     │ 1 : N
    │ (一场培训可多人参加)        │ (一场培训可安排多次考试)
    ▼                           ▼
training_enrollment            exam (考试)
    │                           │
    │ N : 1                     │ 1 : N
    │                           │ (一次考试可产生多条成绩)
    ▼                           ▼
employee ◄───────────────── exam_result
              N : 1
       (一个人员可拥有多条成绩)
```

---

## 4. 人员信息表 `employee`

### 4.1 表说明

用于保存参加培训和考试的人员基本信息。人员的部门和职位可能随时间变动，但此表保存的是当前最新状态。

### 4.2 字段结构

| 字段名             | 数据类型              | 允许为空 | 键类型   | 默认值           | 字段说明         |
| ------------------ | --------------------- | -------- | -------- | ---------------- | ---------------- |
| `id`               | `BIGINT UNSIGNED`     | 否       | 主键     | AUTO_INCREMENT   | 人员唯一编号     |
| `employee_no`      | `VARCHAR(50)`         | 否       | 唯一键   | —                | 工号或学号       |
| `employee_name`    | `VARCHAR(100)`        | 否       | 索引     | —                | 人员姓名         |
| `gender`           | `VARCHAR(10)`         | 是       | —        | `NULL`           | 性别             |
| `department_name`  | `VARCHAR(100)`        | 是       | 索引     | `NULL`           | 当前所属部门     |
| `position_name`    | `VARCHAR(100)`        | 是       | —        | `NULL`           | 当前职位         |
| `phone`            | `VARCHAR(30)`         | 是       | —        | `NULL`           | 联系电话         |
| `email`            | `VARCHAR(255)`        | 是       | —        | `NULL`           | 邮箱地址         |
| `employee_status`  | `VARCHAR(20)`         | 否       | 索引     | `在职`           | 人员当前状态     |
| `hire_date`        | `DATE`                | 是       | —        | `NULL`           | 入职日期         |
| `remarks`          | `TEXT`                | 是       | —        | `NULL`           | 备注信息         |
| `created_at`       | `DATETIME`            | 否       | —        | `CURRENT_TIMESTAMP` | 数据创建时间  |
| `updated_at`       | `DATETIME`            | 否       | —        | `CURRENT_TIMESTAMP ON UPDATE` | 数据最后修改时间 |

### 4.3 约束

| 约束名                   | 类型         | 字段             | 说明       |
| ------------------------ | ------------ | ---------------- | ---------- |
| `PRIMARY`                | 主键         | `id`             | 自增主键   |
| `uk_employee_no`         | 唯一键       | `employee_no`    | 工号唯一   |
| `idx_employee_name`      | 普通索引     | `employee_name`  | —          |
| `idx_department_name`    | 普通索引     | `department_name` | —          |
| `idx_employee_status`    | 普通索引     | `employee_status`  | —          |

### 4.4 字段取值说明

**`employee_status`**

| 值       | 含义     |
| -------- | -------- |
| `在职`   | 在职人员 |
| `离职`   | 已离职   |
| `退休`   | 已退休   |
| `实习`   | 实习生   |
| `其他`   | 其他状态 |

### 4.5 完整建表语句

```sql
CREATE TABLE `employee` (
  `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '人员唯一编号',
  `employee_no`     VARCHAR(50)     NOT NULL                COMMENT '工号或学号',
  `employee_name`   VARCHAR(100)    NOT NULL                COMMENT '人员姓名',
  `gender`          VARCHAR(10)     DEFAULT NULL            COMMENT '性别',
  `department_name` VARCHAR(100)    DEFAULT NULL            COMMENT '当前所属部门',
  `position_name`   VARCHAR(100)    DEFAULT NULL            COMMENT '当前职位',
  `phone`           VARCHAR(30)     DEFAULT NULL            COMMENT '联系电话',
  `email`           VARCHAR(255)    DEFAULT NULL            COMMENT '邮箱地址',
  `employee_status` VARCHAR(20)     NOT NULL DEFAULT '在职' COMMENT '人员当前状态',
  `hire_date`       DATE            DEFAULT NULL            COMMENT '入职日期',
  `remarks`         TEXT                                    COMMENT '备注信息',
  `created_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '数据创建时间',
  `updated_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '数据最后修改时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_employee_no` (`employee_no`),
  KEY `idx_employee_name` (`employee_name`),
  KEY `idx_department_name` (`department_name`),
  KEY `idx_employee_status` (`employee_status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='人员信息表';
```

### 4.6 示例数据

| id | employee_no | employee_name | department_name | position_name | employee_status |
| -: | ----------- | ------------- | --------------- | ------------- | --------------- |
|  1 | EMP001      | 张三          | 技术部          | 软件工程师    | 在职            |
|  2 | EMP002      | 李四          | 人事部          | 人事专员      | 在职            |
|  3 | EMP003      | 王五          | 财务部          | 会计          | 离职            |
|  4 | EMP004      | 赵六          | 技术部          | 高级软件工程师 | 在职            |
|  5 | EMP005      | 孙七          | 管理部          | 部门经理      | 在职            |

---

## 5. 培训课程表 `training_course`

### 5.1 表说明

用于保存培训课程本身的基础信息。**培训课程**只表示课程内容，不表示某一次具体的培训活动。例如"安全生产培训"是一门课程，而"2025年第一期安全生产培训"属于具体培训场次。

### 5.2 字段结构

| 字段名               | 数据类型              | 允许为空 | 键类型   | 默认值           | 字段说明         |
| -------------------- | --------------------- | -------- | -------- | ---------------- | ---------------- |
| `id`                 | `BIGINT UNSIGNED`     | 否       | 主键     | AUTO_INCREMENT   | 课程唯一编号     |
| `course_code`        | `VARCHAR(50)`         | 否       | 唯一键   | —                | 课程编号         |
| `course_name`        | `VARCHAR(200)`        | 否       | 索引     | —                | 课程名称         |
| `course_category`    | `VARCHAR(100)`        | 是       | 索引     | `NULL`           | 课程类别         |
| `standard_hours`     | `DECIMAL(6,2)`        | 是       | —        | `NULL`           | 标准培训学时     |
| `course_level`       | `VARCHAR(30)`         | 是       | —        | `NULL`           | 课程等级         |
| `course_description` | `TEXT`                | 是       | —        | `NULL`           | 课程内容说明     |
| `is_active`          | `TINYINT(1)`          | 否       | —        | `1`              | 是否启用         |
| `created_at`         | `DATETIME`            | 否       | —        | `CURRENT_TIMESTAMP` | 数据创建时间  |
| `updated_at`         | `DATETIME`            | 否       | —        | `CURRENT_TIMESTAMP ON UPDATE` | 数据最后修改时间 |

### 5.3 约束

| 约束名                | 类型     | 字段              | 说明                      |
| --------------------- | -------- | ----------------- | ------------------------- |
| `PRIMARY`             | 主键     | `id`              | 自增主键                  |
| `uk_course_code`      | 唯一键   | `course_code`     | 课程编号唯一              |
| `idx_course_name`     | 普通索引 | `course_name`     | —                         |
| `idx_course_category` | 普通索引 | `course_category` | —                         |
| `chk_standard_hours`  | CHECK    | `standard_hours`  | `NULL` 或 `>= 0`          |

### 5.4 字段取值说明

**`course_level`**

| 值       | 含义     |
| -------- | -------- |
| `初级`   | 初级课程 |
| `中级`   | 中级课程 |
| `高级`   | 高级课程 |
| `通用`   | 通用课程 |
| `其他`   | 其他等级 |

**`is_active`**

| 值 | 含义     |
| -: | -------- |
|  1 | 课程启用 |
|  0 | 课程停用 |

### 5.5 完整建表语句

```sql
CREATE TABLE `training_course` (
  `id`                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '课程唯一编号',
  `course_code`        VARCHAR(50)     NOT NULL                COMMENT '课程编号',
  `course_name`        VARCHAR(200)    NOT NULL                COMMENT '课程名称',
  `course_category`    VARCHAR(100)    DEFAULT NULL            COMMENT '课程类别',
  `standard_hours`     DECIMAL(6,2)    DEFAULT NULL            COMMENT '标准培训学时',
  `course_level`       VARCHAR(30)     DEFAULT NULL            COMMENT '课程等级',
  `course_description` TEXT                                    COMMENT '课程内容说明',
  `is_active`          TINYINT(1)      NOT NULL DEFAULT 1      COMMENT '是否启用（1=启用，0=停用）',
  `created_at`         DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '数据创建时间',
  `updated_at`         DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '数据最后修改时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_course_code` (`course_code`),
  KEY `idx_course_name` (`course_name`),
  KEY `idx_course_category` (`course_category`),
  CONSTRAINT `chk_standard_hours` CHECK ((`standard_hours` IS NULL) OR (`standard_hours` >= 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='培训课程表';
```

### 5.6 示例数据

| id | course_code | course_name          | course_category | standard_hours | course_level |
| -: | ----------- | -------------------- | --------------- | -------------: | ------------ |
|  1 | SAFE-001    | 安全生产基础培训     | 安全培训        |           8.00 | 初级         |
|  2 | NET-001     | 网络安全意识培训     | 信息安全        |           6.00 | 通用         |
|  3 | FIRE-001    | 消防安全培训         | 安全培训        |           4.00 | 通用         |
|  4 | MGMT-001    | 中层管理技能提升     | 管理培训        |          16.00 | 中级         |
|  5 | TECH-001    | Python数据分析进阶   | 技术培训        |          24.00 | 高级         |

---

## 6. 培训场次表 `training_session`

### 6.1 表说明

用于保存某门课程在某一年实际举办的一次培训活动。同一门课程可以在不同年份举办多次，因此课程与场次分离——课程定义"培训什么"，场次记录"何时何地举办"。

### 6.2 字段结构

| 字段名               | 数据类型              | 允许为空 | 键类型   | 默认值           | 字段说明           |
| -------------------- | --------------------- | -------- | -------- | ---------------- | ------------------ |
| `id`                 | `BIGINT UNSIGNED`     | 否       | 主键     | AUTO_INCREMENT   | 培训场次唯一编号   |
| `course_id`          | `BIGINT UNSIGNED`     | 否       | 外键, 索引 | —              | 对应的培训课程编号 |
| `training_year`      | `YEAR`                | 否       | 索引     | —                | 培训年度           |
| `session_no`         | `VARCHAR(50)`         | 否       | 唯一键   | —                | 培训场次编号       |
| `session_name`       | `VARCHAR(200)`        | 否       | —        | —                | 培训场次名称       |
| `start_date`         | `DATE`                | 否       | 索引     | —                | 培训开始日期       |
| `end_date`           | `DATE`                | 否       | —        | —                | 培训结束日期       |
| `actual_hours`       | `DECIMAL(6,2)`        | 是       | —        | `NULL`           | 实际培训学时       |
| `trainer_name`       | `VARCHAR(100)`        | 是       | —        | `NULL`           | 培训讲师姓名       |
| `organizer`          | `VARCHAR(200)`        | 是       | —        | `NULL`           | 培训组织单位       |
| `training_location`  | `VARCHAR(255)`        | 是       | —        | `NULL`           | 培训地点           |
| `training_method`    | `VARCHAR(30)`         | 否       | —        | `线下`           | 培训方式           |
| `session_status`     | `VARCHAR(30)`         | 否       | 索引     | `计划中`         | 培训状态           |
| `planned_count`      | `INT UNSIGNED`        | 是       | —        | `NULL`           | 计划参加人数       |
| `participant_count`  | `INT UNSIGNED`        | 是       | —        | `NULL`           | 实际参加人数       |
| `training_fee`       | `DECIMAL(10,2)`       | 是       | —        | `NULL`           | 培训总费用         |
| `remarks`            | `TEXT`                | 是       | —        | `NULL`           | 备注信息           |
| `created_at`         | `DATETIME`            | 否       | —        | `CURRENT_TIMESTAMP` | 数据创建时间    |
| `updated_at`         | `DATETIME`            | 否       | —        | `CURRENT_TIMESTAMP ON UPDATE` | 数据最后修改时间 |

### 6.3 约束

| 约束名                   | 类型     | 字段                  | 说明                                    |
| ------------------------ | -------- | --------------------- | --------------------------------------- |
| `PRIMARY`                | 主键     | `id`                  | 自增主键                                |
| `uk_session_no`          | 唯一键   | `session_no`          | 场次编号唯一                            |
| `fk_session_course`      | 外键     | `course_id`           | → `training_course.id`, RESTRICT/CASCADE |
| `idx_course_id`          | 普通索引 | `course_id`           | —                                       |
| `idx_training_year`      | 普通索引 | `training_year`       | —                                       |
| `idx_start_date`         | 普通索引 | `start_date`          | —                                       |
| `idx_session_status`     | 普通索引 | `session_status`      | —                                       |
| `chk_end_date`           | CHECK    | `end_date`            | `>= start_date`                         |
| `chk_actual_hours`       | CHECK    | `actual_hours`        | `NULL` 或 `>= 0`                        |
| `chk_planned_count`      | CHECK    | `planned_count`       | `NULL` 或 `>= 0`                        |
| `chk_participant_count`  | CHECK    | `participant_count`   | `NULL` 或 `>= 0`                        |
| `chk_training_fee`       | CHECK    | `training_fee`        | `NULL` 或 `>= 0`                        |

### 6.4 字段取值说明

**`training_method`**

| 值               | 含义           |
| ---------------- | -------------- |
| `线下`           | 线下面授       |
| `线上`           | 线上培训       |
| `线上线下结合`   | 混合式培训     |
| `自学`           | 自学           |
| `其他`           | 其他方式       |

**`session_status`**

| 值       | 含义         |
| -------- | ------------ |
| `计划中` | 培训已计划   |
| `报名中` | 正在接受报名 |
| `进行中` | 培训进行中   |
| `已结束` | 培训已完成   |
| `已取消` | 培训已取消   |

### 6.5 完整建表语句

```sql
CREATE TABLE `training_session` (
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
  `remarks`           TEXT                                    COMMENT '备注信息',
  `created_at`        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '数据创建时间',
  `updated_at`        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '数据最后修改时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_session_no` (`session_no`),
  KEY `idx_course_id` (`course_id`),
  KEY `idx_training_year` (`training_year`),
  KEY `idx_start_date` (`start_date`),
  KEY `idx_session_status` (`session_status`),
  CONSTRAINT `fk_session_course` FOREIGN KEY (`course_id`) REFERENCES `training_course` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT `chk_end_date`          CHECK ((`end_date` >= `start_date`)),
  CONSTRAINT `chk_actual_hours`      CHECK ((`actual_hours` IS NULL) OR (`actual_hours` >= 0)),
  CONSTRAINT `chk_planned_count`     CHECK ((`planned_count` IS NULL) OR (`planned_count` >= 0)),
  CONSTRAINT `chk_participant_count` CHECK ((`participant_count` IS NULL) OR (`participant_count` >= 0)),
  CONSTRAINT `chk_training_fee`      CHECK ((`training_fee` IS NULL) OR (`training_fee` >= 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='培训场次表';
```

### 6.6 示例数据

| id | course_id | training_year | session_no    | session_name                     | start_date  | end_date    | session_status |
| -: | --------: | ------------: | ------------- | -------------------------------- | ----------- | ----------- | -------------- |
|  1 |         1 |          2025 | TS-2025-001   | 2025年第一期安全生产基础培训     | 2025-05-10  | 2025-05-11  | 已结束         |
|  2 |         2 |          2024 | TS-2024-001   | 2024年网络安全意识培训           | 2024-09-15  | 2024-09-15  | 已结束         |
|  3 |         3 |          2023 | TS-2023-001   | 2023年消防安全培训               | 2023-06-20  | 2023-06-20  | 已结束         |
|  7 |         2 |          2025 | TS-2025-004   | 2025年网络安全意识培训           | 2025-09-01  | 2025-09-01  | 报名中         |
| 12 |         1 |          2023 | TS-2023-002   | 2023年第一期安全生产基础培训     | 2023-05-06  | 2023-05-07  | 已取消         |

---

## 7. 人员培训记录表 `training_enrollment`

### 7.1 表说明

用于保存人员参加培训的情况。人员与培训场次是多对多关系——一个人可参加多场培训，一场培训可有多人参加。此表还保存参加培训时的部门和职位快照，用于追溯历史状态。

### 7.2 字段结构

| 字段名                 | 数据类型              | 允许为空 | 键类型   | 默认值           | 字段说明               |
| ---------------------- | --------------------- | -------- | -------- | ---------------- | ---------------------- |
| `id`                   | `BIGINT UNSIGNED`     | 否       | 主键     | AUTO_INCREMENT   | 培训记录唯一编号       |
| `session_id`           | `BIGINT UNSIGNED`     | 否       | 外键, 索引 | —              | 培训场次编号           |
| `employee_id`          | `BIGINT UNSIGNED`     | 否       | 外键, 索引 | —              | 参加培训的人员编号     |
| `signup_date`          | `DATE`                | 是       | —        | `NULL`           | 报名日期               |
| `attendance_hours`     | `DECIMAL(6,2)`        | 是       | —        | `NULL`           | 实际出勤学时           |
| `attendance_rate`      | `DECIMAL(5,2)`        | 是       | —        | `NULL`           | 出勤率（0.00~100.00）  |
| `completion_status`    | `VARCHAR(30)`         | 否       | 索引     | `已报名`         | 培训完成状态           |
| `completion_date`      | `DATE`                | 是       | —        | `NULL`           | 培训完成日期           |
| `evaluation_score`     | `DECIMAL(5,2)`        | 是       | —        | `NULL`           | 培训评价分数           |
| `evaluation`           | `VARCHAR(500)`        | 是       | —        | `NULL`           | 培训评价内容           |
| `certificate_no`       | `VARCHAR(100)`        | 是       | —        | `NULL`           | 培训证书编号           |
| `certificate_date`     | `DATE`                | 是       | —        | `NULL`           | 证书发放日期           |
| `department_snapshot`  | `VARCHAR(100)`        | 是       | 索引     | `NULL`           | 参加培训时所在部门     |
| `position_snapshot`    | `VARCHAR(100)`        | 是       | —        | `NULL`           | 参加培训时的职位       |
| `remarks`              | `TEXT`                | 是       | —        | `NULL`           | 备注信息               |
| `created_at`           | `DATETIME`            | 否       | —        | `CURRENT_TIMESTAMP` | 数据创建时间        |
| `updated_at`           | `DATETIME`            | 否       | —        | `CURRENT_TIMESTAMP ON UPDATE` | 数据最后修改时间 |

### 7.3 约束

| 约束名                        | 类型     | 字段                        | 说明                                      |
| ----------------------------- | -------- | --------------------------- | ----------------------------------------- |
| `PRIMARY`                     | 主键     | `id`                        | 自增主键                                  |
| `uk_session_employee`         | 唯一键   | `(session_id, employee_id)` | 同一人不能重复参加同一场培训              |
| `fk_enrollment_session`       | 外键     | `session_id`                | → `training_session.id`, RESTRICT/CASCADE |
| `fk_enrollment_employee`      | 外键     | `employee_id`               | → `employee.id`, RESTRICT/CASCADE         |
| `idx_session_id`              | 普通索引 | `session_id`                | —                                         |
| `idx_employee_id`             | 普通索引 | `employee_id`               | —                                         |
| `idx_completion_status`       | 普通索引 | `completion_status`         | —                                         |
| `idx_department_snapshot`     | 普通索引 | `department_snapshot`       | —                                         |
| `chk_attendance_rate`         | CHECK    | `attendance_rate`           | `NULL` 或 `0.00 ~ 100.00`                 |

### 7.4 字段取值说明

**`completion_status`**

| 值       | 含义         |
| -------- | ------------ |
| `已报名` | 已报名未参加 |
| `培训中` | 正在参加培训 |
| `已完成` | 已完成培训   |
| `未完成` | 未完成培训   |
| `缺席`   | 缺席培训     |
| `已取消` | 报名已取消   |

**`attendance_rate`** — 百分数，取值范围 `0.00 ~ 100.00`

| attendance_rate | 含义         |
| --------------: | ------------ |
|          100.00 | 全勤         |
|           80.00 | 出勤率 80%   |
|            0.00 | 完全缺席     |

### 7.5 历史快照说明

`department_snapshot` 和 `position_snapshot` 用于保存人员参加培训时的部门和职位。即使人员在 `employee` 表中的当前部门已经变更，历史培训记录仍能通过快照字段还原当时的状态。

> **示例：** 王五（EMP003）2023年参加培训时属于财务部-会计。2024年离职后 `employee` 表状态更新为"离职"，但其 2023 年的培训记录快照依然是"财务部-会计"。

### 7.6 完整建表语句

```sql
CREATE TABLE `training_enrollment` (
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
  `remarks`             TEXT                                    COMMENT '备注信息',
  `created_at`          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '数据创建时间',
  `updated_at`          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '数据最后修改时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_session_employee` (`session_id`, `employee_id`),
  KEY `idx_session_id` (`session_id`),
  KEY `idx_employee_id` (`employee_id`),
  KEY `idx_completion_status` (`completion_status`),
  KEY `idx_department_snapshot` (`department_snapshot`),
  CONSTRAINT `fk_enrollment_session`  FOREIGN KEY (`session_id`)  REFERENCES `training_session` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT `fk_enrollment_employee` FOREIGN KEY (`employee_id`) REFERENCES `employee` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT `chk_attendance_rate` CHECK ((`attendance_rate` IS NULL) OR ((`attendance_rate` >= 0) AND (`attendance_rate` <= 100)))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='人员培训记录表';
```

### 7.7 示例数据

| id | session_id | employee_id | attendance_hours | attendance_rate | completion_status |
| -: | ---------: | ----------: | ---------------: | --------------: | ----------------- |
|  1 |          1 |           1 |             8.00 |          100.00 | 已完成            |
|  2 |          1 |           2 |             6.00 |           75.00 | 已完成            |
|  5 |          1 |           7 |             4.00 |           50.00 | 未完成            |
| 10 |          2 |          12 |             0.00 |            0.00 | 缺席              |
| 35 |         12 |           1 |             NULL |            NULL | 已取消            |

---

## 8. 考试信息表 `exam`

### 8.1 表说明

用于保存培训对应的考试安排。一场培训可以没有考试，也可以安排多次考试（结业考试、补考、认证考试、阶段考试等）。

### 8.2 字段结构

| 字段名               | 数据类型              | 允许为空 | 键类型   | 默认值           | 字段说明             |
| -------------------- | --------------------- | -------- | -------- | ---------------- | -------------------- |
| `id`                 | `BIGINT UNSIGNED`     | 否       | 主键     | AUTO_INCREMENT   | 考试唯一编号         |
| `session_id`         | `BIGINT UNSIGNED`     | 否       | 外键, 索引 | —              | 对应的培训场次编号   |
| `exam_no`            | `VARCHAR(50)`         | 否       | 唯一键   | —                | 考试编号             |
| `exam_name`          | `VARCHAR(200)`        | 否       | —        | —                | 考试名称             |
| `exam_type`          | `VARCHAR(30)`         | 否       | 索引     | `结业考试`       | 考试类型             |
| `exam_date`          | `DATETIME`            | 否       | 索引     | —                | 考试日期和时间       |
| `duration_minutes`   | `INT UNSIGNED`        | 是       | —        | `NULL`           | 考试时长（分钟）     |
| `total_score`        | `DECIMAL(6,2)`        | 否       | —        | `100.00`         | 考试总分             |
| `pass_score`         | `DECIMAL(6,2)`        | 否       | —        | `60.00`          | 及格分数             |
| `exam_method`        | `VARCHAR(30)`         | 否       | —        | `闭卷`           | 考试方式             |
| `exam_location`      | `VARCHAR(255)`        | 是       | —        | `NULL`           | 考试地点             |
| `exam_status`        | `VARCHAR(30)`         | 否       | 索引     | `未开始`         | 考试状态             |
| `paper_count`        | `INT UNSIGNED`        | 是       | —        | `NULL`           | 试卷数量             |
| `invigilator`        | `VARCHAR(100)`        | 是       | —        | `NULL`           | 监考人员             |
| `remarks`            | `TEXT`                | 是       | —        | `NULL`           | 备注信息             |
| `created_at`         | `DATETIME`            | 否       | —        | `CURRENT_TIMESTAMP` | 数据创建时间      |
| `updated_at`         | `DATETIME`            | 否       | —        | `CURRENT_TIMESTAMP ON UPDATE` | 数据最后修改时间 |

### 8.3 约束

| 约束名                  | 类型     | 字段               | 说明                                      |
| ----------------------- | -------- | ------------------ | ----------------------------------------- |
| `PRIMARY`               | 主键     | `id`               | 自增主键                                  |
| `uk_exam_no`            | 唯一键   | `exam_no`          | 考试编号唯一                              |
| `fk_exam_session`       | 外键     | `session_id`       | → `training_session.id`, RESTRICT/CASCADE |
| `idx_session_id`        | 普通索引 | `session_id`       | —                                         |
| `idx_exam_date`         | 普通索引 | `exam_date`        | —                                         |
| `idx_exam_type`         | 普通索引 | `exam_type`        | —                                         |
| `idx_exam_status`       | 普通索引 | `exam_status`      | —                                         |
| `chk_duration_minutes`  | CHECK    | `duration_minutes` | `NULL` 或 `>= 0`                          |
| `chk_total_score`       | CHECK    | `total_score`      | `> 0`                                     |
| `chk_pass_score`        | CHECK    | `pass_score`       | `>= 0 AND <= total_score`                 |
| `chk_paper_count`       | CHECK    | `paper_count`      | `NULL` 或 `>= 0`                          |

### 8.4 字段取值说明

**`exam_type`**

| 值         | 含义       |
| ---------- | ---------- |
| `结业考试` | 培训结业考 |
| `补考`     | 补考       |
| `认证考试` | 资格认证考 |
| `阶段考试` | 阶段测验   |
| `入学考试` | 入学考     |
| `其他`     | 其他类型   |

**`exam_method`**

| 值     | 含义     |
| ------ | -------- |
| `闭卷` | 闭卷笔试 |
| `开卷` | 开卷笔试 |
| `机考` | 上机考试 |
| `实操` | 实际操作 |
| `线上` | 线上考试 |
| `面试` | 面试     |
| `其他` | 其他方式 |

**`exam_status`**

| 值       | 含义     |
| -------- | -------- |
| `未开始` | 尚未开始 |
| `进行中` | 考试中   |
| `已结束` | 已结束   |
| `已取消` | 已取消   |

### 8.5 完整建表语句

```sql
CREATE TABLE `exam` (
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
  `remarks`          TEXT                                    COMMENT '备注信息',
  `created_at`       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '数据创建时间',
  `updated_at`       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '数据最后修改时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_exam_no` (`exam_no`),
  KEY `idx_session_id` (`session_id`),
  KEY `idx_exam_date` (`exam_date`),
  KEY `idx_exam_type` (`exam_type`),
  KEY `idx_exam_status` (`exam_status`),
  CONSTRAINT `fk_exam_session` FOREIGN KEY (`session_id`) REFERENCES `training_session` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT `chk_duration_minutes` CHECK ((`duration_minutes` IS NULL) OR (`duration_minutes` >= 0)),
  CONSTRAINT `chk_total_score`      CHECK (`total_score` > 0),
  CONSTRAINT `chk_pass_score`       CHECK ((`pass_score` >= 0) AND (`pass_score` <= `total_score`)),
  CONSTRAINT `chk_paper_count`      CHECK ((`paper_count` IS NULL) OR (`paper_count` >= 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='考试信息表';
```

### 8.6 示例数据

| id | session_id | exam_no      | exam_name                        | exam_type | total_score | pass_score | exam_status |
| -: | ---------: | ------------ | -------------------------------- | --------- | ----------: | ---------: | ----------- |
|  1 |          1 | EX-2025-001  | 2025年安全生产培训结业考试       | 结业考试  |      100.00 |      60.00 | 已结束      |
|  2 |          1 | EX-2025-002  | 2025年安全生产培训补考           | 补考      |      100.00 |      60.00 | 已结束      |
|  4 |          4 | EX-2025-003  | 2025年管理技能培训结业考试       | 结业考试  |      100.00 |      70.00 | 已结束      |
|  8 |         10 | EX-2025-006  | 2025年质量体系内审员认证         | 认证考试  |      120.00 |      84.00 | 未开始      |

---

## 9. 考试成绩表 `exam_result`

### 9.1 表说明

用于保存每个人参加考试后的成绩和考试结果。一次考试可有多人参加，一个人员也可参加多次考试（含补考）。`is_best_result` 字段标记该人员在此次考试中的最佳成绩。

### 9.2 字段结构

| 字段名             | 数据类型              | 允许为空 | 键类型   | 默认值           | 字段说明               |
| ------------------ | --------------------- | -------- | -------- | ---------------- | ---------------------- |
| `id`               | `BIGINT UNSIGNED`     | 否       | 主键     | AUTO_INCREMENT   | 成绩记录唯一编号       |
| `exam_id`          | `BIGINT UNSIGNED`     | 否       | 外键, 索引 | —              | 考试编号               |
| `employee_id`      | `BIGINT UNSIGNED`     | 否       | 外键, 索引 | —              | 参加考试的人员编号     |
| `attempt_no`       | `INT UNSIGNED`        | 否       | —        | `1`              | 第几次参加考试         |
| `score`            | `DECIMAL(6,2)`        | 是       | 索引     | `NULL`           | 考试成绩               |
| `result_status`    | `VARCHAR(30)`         | 否       | 索引     | `未考试`         | 考试结果               |
| `answer_sheet_no`  | `VARCHAR(100)`        | 是       | —        | `NULL`           | 答题卡编号             |
| `paper_no`         | `VARCHAR(100)`        | 是       | —        | `NULL`           | 试卷编号               |
| `seat_no`          | `VARCHAR(30)`         | 是       | —        | `NULL`           | 考场座位号             |
| `absent_reason`    | `VARCHAR(500)`        | 是       | —        | `NULL`           | 缺考原因               |
| `graded_at`        | `DATETIME`            | 是       | —        | `NULL`           | 成绩登记时间           |
| `grader_name`      | `VARCHAR(100)`        | 是       | —        | `NULL`           | 阅卷或成绩登记人员     |
| `is_best_result`   | `TINYINT(1)`          | 否       | 索引     | `0`              | 是否为最佳成绩         |
| `remarks`          | `TEXT`                | 是       | —        | `NULL`           | 备注信息               |
| `created_at`       | `DATETIME`            | 否       | —        | `CURRENT_TIMESTAMP` | 数据创建时间        |
| `updated_at`       | `DATETIME`            | 否       | —        | `CURRENT_TIMESTAMP ON UPDATE` | 数据最后修改时间 |

### 9.3 约束

| 约束名                          | 类型     | 字段                                | 说明                                      |
| ------------------------------- | -------- | ----------------------------------- | ----------------------------------------- |
| `PRIMARY`                       | 主键     | `id`                                | 自增主键                                  |
| `uk_exam_employee_attempt`      | 唯一键   | `(exam_id, employee_id, attempt_no)` | 同人同考试同次数记录唯一                  |
| `fk_result_exam`                | 外键     | `exam_id`                           | → `exam.id`, RESTRICT/CASCADE             |
| `fk_result_employee`            | 外键     | `employee_id`                       | → `employee.id`, RESTRICT/CASCADE         |
| `idx_exam_id`                   | 普通索引 | `exam_id`                           | —                                         |
| `idx_employee_id`               | 普通索引 | `employee_id`                       | —                                         |
| `idx_result_status`             | 普通索引 | `result_status`                     | —                                         |
| `idx_score`                     | 普通索引 | `score`                             | —                                         |
| `idx_is_best_result`            | 普通索引 | `is_best_result`                    | —                                         |
| `chk_attempt_no`                | CHECK    | `attempt_no`                        | `>= 1`                                    |
| `chk_score`                     | CHECK    | `score`                             | `NULL` 或 `>= 0`                          |

### 9.4 字段取值说明

**`result_status`**

| 值         | 含义       |
| ---------- | ---------- |
| `未考试`   | 未参加考试 |
| `考试中`   | 正在考试   |
| `合格`     | 考试合格   |
| `不合格`   | 考试不合格 |
| `缺考`     | 缺考       |
| `作弊`     | 考试作弊   |
| `成绩作废` | 成绩作废   |

**`is_best_result`**

| 值 | 含义                   |
| -: | ---------------------- |
|  1 | 当前记录为该人最佳成绩 |
|  0 | 非最佳成绩             |

### 9.5 业务规则

- 如果 `result_status` 为 `缺考`，则 `score` 通常应为 `NULL`
- 如果 `result_status` 为 `合格` 或 `不合格`，则 `score` 应有具体数值
- 如果记录被标记为最佳成绩，则 `is_best_result = 1`；同一人在同一考试中仅一条记录可为 `1`
- `attempt_no >= 1`，补考时递增

### 9.6 完整建表语句

```sql
CREATE TABLE `exam_result` (
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
  `remarks`         TEXT                                    COMMENT '备注信息',
  `created_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '数据创建时间',
  `updated_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '数据最后修改时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_exam_employee_attempt` (`exam_id`, `employee_id`, `attempt_no`),
  KEY `idx_exam_id` (`exam_id`),
  KEY `idx_employee_id` (`employee_id`),
  KEY `idx_result_status` (`result_status`),
  KEY `idx_score` (`score`),
  KEY `idx_is_best_result` (`is_best_result`),
  CONSTRAINT `fk_result_exam`     FOREIGN KEY (`exam_id`)     REFERENCES `exam` (`id`)     ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT `fk_result_employee` FOREIGN KEY (`employee_id`) REFERENCES `employee` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT `chk_attempt_no` CHECK (`attempt_no` >= 1),
  CONSTRAINT `chk_score`      CHECK ((`score` IS NULL) OR (`score` >= 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='考试成绩表';
```

### 9.7 示例数据

| id | exam_id | employee_id | attempt_no | score  | result_status | is_best_result |
| -: | ------: | ----------: | ---------: | -----: | ------------- | -------------: |
|  1 |       1 |           1 |          1 |  86.00 | 合格          |              0 |
|  2 |       1 |           2 |          1 |  55.00 | 不合格        |              0 |
|  6 |       2 |           2 |          2 |  78.00 | 合格          |              1 |
| 11 |       3 |          12 |          1 |   NULL | 缺考          |              0 |
| 26 |       7 |          11 |          1 |  92.00 | 作弊          |              0 |
| 27 |       2 |           1 |          2 |  94.00 | 合格          |              1 |

---

## 10. 外键关系汇总

| 子表                   | 外键字段        | 父表                | 父表字段 | 删除策略  | 更新策略  |
| ---------------------- | --------------- | ------------------- | -------- | --------- | --------- |
| `training_session`     | `course_id`     | `training_course`   | `id`     | RESTRICT  | CASCADE   |
| `training_enrollment`  | `session_id`    | `training_session`  | `id`     | RESTRICT  | CASCADE   |
| `training_enrollment`  | `employee_id`   | `employee`          | `id`     | RESTRICT  | CASCADE   |
| `exam`                 | `session_id`    | `training_session`  | `id`     | RESTRICT  | CASCADE   |
| `exam_result`          | `exam_id`       | `exam`              | `id`     | RESTRICT  | CASCADE   |
| `exam_result`          | `employee_id`   | `employee`          | `id`     | RESTRICT  | CASCADE   |

> **RESTRICT**：禁止在有子记录时删除父记录
> **CASCADE**：父表主键变更时自动级联更新子表

---

## 11. 约束汇总

### 11.1 主键

| 表名                   | 主键字段 |
| ---------------------- | -------- |
| `employee`             | `id`     |
| `training_course`      | `id`     |
| `training_session`     | `id`     |
| `training_enrollment`  | `id`     |
| `exam`                 | `id`     |
| `exam_result`          | `id`     |

### 11.2 唯一约束

| 表名                   | 唯一字段或字段组合                          | 说明                   |
| ---------------------- | ------------------------------------------ | ---------------------- |
| `employee`             | `employee_no`                              | 工号不能重复           |
| `training_course`      | `course_code`                              | 课程编号不能重复       |
| `training_session`     | `session_no`                               | 场次编号不能重复       |
| `training_enrollment`  | `(session_id, employee_id)`                | 同人不能重复参加同场   |
| `exam`                 | `exam_no`                                  | 考试编号不能重复       |
| `exam_result`          | `(exam_id, employee_id, attempt_no)`       | 同人同考试同次数唯一   |

### 11.3 CHECK 约束

| 表名                   | 约束名                  | 规则                                          |
| ---------------------- | ----------------------- | --------------------------------------------- |
| `training_course`      | `chk_standard_hours`    | `standard_hours IS NULL OR >= 0`              |
| `training_session`     | `chk_end_date`          | `end_date >= start_date`                      |
| `training_session`     | `chk_actual_hours`      | `actual_hours IS NULL OR >= 0`                |
| `training_session`     | `chk_planned_count`     | `planned_count IS NULL OR >= 0`               |
| `training_session`     | `chk_participant_count` | `participant_count IS NULL OR >= 0`           |
| `training_session`     | `chk_training_fee`      | `training_fee IS NULL OR >= 0`                |
| `training_enrollment`  | `chk_attendance_rate`   | `attendance_rate IS NULL OR (>= 0 AND <= 100)` |
| `exam`                 | `chk_duration_minutes`  | `duration_minutes IS NULL OR >= 0`            |
| `exam`                 | `chk_total_score`       | `total_score > 0`                             |
| `exam`                 | `chk_pass_score`        | `pass_score >= 0 AND pass_score <= total_score` |
| `exam`                 | `chk_paper_count`       | `paper_count IS NULL OR >= 0`                 |
| `exam_result`          | `chk_attempt_no`        | `attempt_no >= 1`                             |
| `exam_result`          | `chk_score`             | `score IS NULL OR >= 0`                       |

---

## 12. 索引汇总

### 12.1 按表统计

| 表名                   | 普通索引                                                                 |
| ---------------------- | ------------------------------------------------------------------------ |
| `employee`             | `idx_employee_name`, `idx_department_name`, `idx_employee_status`        |
| `training_course`      | `idx_course_name`, `idx_course_category`                                 |
| `training_session`     | `idx_course_id`, `idx_training_year`, `idx_start_date`, `idx_session_status` |
| `training_enrollment`  | `idx_session_id`, `idx_employee_id`, `idx_completion_status`, `idx_department_snapshot` |
| `exam`                 | `idx_session_id`, `idx_exam_date`, `idx_exam_type`, `idx_exam_status`    |
| `exam_result`          | `idx_exam_id`, `idx_employee_id`, `idx_result_status`, `idx_score`, `idx_is_best_result` |

### 12.2 索引命名规范

| 索引类型 | 前缀      | 示例                       |
| -------- | --------- | -------------------------- |
| 主键     | `PRIMARY` | `PRIMARY`                  |
| 唯一索引 | `uk_`     | `uk_employee_no`           |
| 普通索引 | `idx_`    | `idx_employee_name`        |
| 外键索引 | `fk_`     | `fk_session_course`        |
| CHECK    | `chk_`    | `chk_end_date`             |

---

## 13. 数据统计

| 表名                   | 记录数 | 备注                                                         |
| ---------------------- | -----: | ------------------------------------------------------------ |
| `employee`             |     15 | 在职 11、离职 1、退休 1、实习 1、其他 1                       |
| `training_course`      |      8 | 安全类 3、技术类 1、管理类 1、法律类 1、质量类 1、健康类 1    |
| `training_session`     |     12 | 2025年 6 场、2024年 4 场、2023年 2 场；含已取消 1 场          |
| `training_enrollment`  |     38 | 已完成 26、培训中 4、未完成 2、缺席 1、已报名 1、已取消 2     |
| `exam`                 |      8 | 结业考试 5、补考 1、认证考试 1、阶段考试 1                     |
| `exam_result`          |     27 | 合格 17、不合格 4、缺考 1、作弊 1、未考试 4                    |

---

## 14. 测试数据覆盖场景

| #  | 场景                     | 验证要点                                                     |
| -: | ------------------------ | ------------------------------------------------------------ |
|  1 | 按年度查询培训           | 2025年6场培训，按日期排序                                     |
|  2 | 课程历年场次             | 安全生产培训 2023/2024/2025 三年，含已取消场次                |
|  3 | 人员培训履历             | 张三参加8场培训，含培训中、已完成、已取消                      |
|  4 | 部门培训统计             | 技术部 2023-2025 年 8 次培训统计，含完成/未完成/缺席          |
|  5 | 培训参加人员名单         | 2025年安全生产培训 5 人参加                                   |
|  6 | 培训出勤率               | 平均 85%，全勤 3 人、部分出勤 2 人                            |
|  7 | 考试安排                 | 同一培训有结业考试+补考两场考试                                |
|  8 | 考试成绩                 | 最高 92 分（赵六），最低 55 分（李四）                         |
|  9 | 合格/不合格统计          | 合格 4 人，不合格 1 人                                        |
| 10 | 年度考试通过率           | 补考 100% > 安全生产 80% > Python 75% > 劳动法 75% > 管理 66.67% |
| 11 | 补考情况                 | 李四首考 55→补考 78 及格；张三首考 86→补考 94 提高             |
| 12 | 最佳成绩标记             | `is_best_result=1` 正确标记每人每场考试的最佳成绩               |
| 13 | 缺考人员及原因           | 黄十四因出差未归缺考                                          |
| 14 | 历史快照                 | 王五(已离职)的培训快照仍显示"财务部-会计"                      |
| 15 | 课程跨年举办             | 安全生产培训 2023/2024/2025 三年均有场次                       |
| 16 | 培训/考试取消            | 2023年安全生产培训因疫情取消，报名记录同步更新                  |
| 17 | 多次考试                 | 李四安全生产考试 2 次（首考+补考），张三 2 次                  |
| 18 | 证书管理                 | 28 条证书记录，按发放日期倒序                                  |

---

## 15. 常用查询示例

### 15.1 按年度查询全部培训

```sql
SELECT s.session_no, s.session_name, c.course_name, s.start_date, s.end_date, s.session_status
FROM training_session s
JOIN training_course c ON s.course_id = c.id
WHERE s.training_year = 2025
ORDER BY s.start_date;
```

### 15.2 查询某人的全部培训历史

```sql
SELECT e.employee_name, s.training_year, s.session_name, c.course_name,
       en.attendance_hours, en.attendance_rate, en.completion_status
FROM training_enrollment en
JOIN employee e ON en.employee_id = e.id
JOIN training_session s ON en.session_id = s.id
JOIN training_course c ON s.course_id = c.id
WHERE e.employee_no = 'EMP001'
ORDER BY s.training_year DESC;
```

### 15.3 统计某年度考试通过率

```sql
SELECT ex.exam_name,
       COUNT(*) AS total,
       SUM(CASE WHEN er.result_status = '合格' THEN 1 ELSE 0 END) AS passed,
       ROUND(AVG(CASE WHEN er.result_status = '合格' THEN 100 ELSE 0 END), 2) AS pass_rate
FROM exam_result er
JOIN exam ex ON er.exam_id = ex.id
JOIN training_session s ON ex.session_id = s.id
WHERE s.training_year = 2025 AND er.result_status IN ('合格', '不合格')
GROUP BY ex.exam_name
ORDER BY pass_rate DESC;
```

### 15.4 查询补考记录

```sql
SELECT emp.employee_name, ex.exam_name, er.attempt_no, er.score, er.result_status
FROM exam_result er
JOIN employee emp ON er.employee_id = emp.id
JOIN exam ex ON er.exam_id = ex.id
WHERE er.attempt_no > 1
ORDER BY emp.employee_no;
```

### 15.5 查询有证书的人员列表

```sql
SELECT emp.employee_no, emp.employee_name, c.course_name,
       en.certificate_no, en.certificate_date
FROM training_enrollment en
JOIN employee emp ON en.employee_id = emp.id
JOIN training_session s ON en.session_id = s.id
JOIN training_course c ON s.course_id = c.id
WHERE en.certificate_no IS NOT NULL
ORDER BY en.certificate_date DESC;
```
