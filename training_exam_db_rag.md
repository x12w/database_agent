# 历年培训与考试信息数据库结构文档

## 文档概述

本文档描述了一个用于管理企业培训与考试信息的 MySQL 关系型数据库 `training_exam_db` 的完整结构。数据库涵盖人员管理、培训课程定义、培训场次安排、人员参训记录、考试安排和考试成绩六大业务领域。文档面向需要了解数据库结构、编写查询语句或进行数据分析的开发人员和业务人员。文档包含每张表的字段定义、数据类型、约束规则、索引策略、表间关系、枚举值说明、业务规则和常用查询示例。

数据库名称：training_exam_db
数据库类型：MySQL 8.0 及以上版本
字符集：utf8mb4，排序规则 utf8mb4_0900_ai_ci
存储引擎：InnoDB
数据表数量：6 张
外键约束数量：6 条
CHECK 约束数量：14 条
唯一约束数量：6 条
索引数量：24 个（包含主键和唯一索引）
测试数据总量：人员 15 条、课程 8 条、场次 12 条、参训记录 38 条、考试 8 条、成绩 27 条

关键词：培训管理、考试管理、MySQL数据库、培训场次、考试成绩、人员管理、培训课程、出勤率、证书管理、补考管理、历史快照、部门培训统计

## 数据库整体架构和表间关系

数据库包含六张业务表，围绕培训业务的两个核心实体展开：培训课程和人员。培训课程（training_course）定义课程内容本身，培训场次（training_session）记录课程在特定年度的具体举办活动，二者是一对多关系——一门课程可以在不同年份举办多次。人员（employee）与培训场次之间通过培训记录表（training_enrollment）建立多对多关系——一人可参加多场培训，一场培训可有多人参加。培训场次可以安排多次考试（exam），一次考试产生多条成绩记录（exam_result），成绩通过人员编号关联到具体考生。

实体关系的方向和数据流向如下：
培训课程 training_course 是顶层实体，定义培训内容。一门课程对应多个培训场次 training_session。培训场次有两个下游分支：一是人员参训方向，通过 training_enrollment 连接 employee，记录谁参加了培训、出勤情况、是否获得证书；二是考试方向，通过 exam 连接培训场次，记录考试安排。考试成绩表 exam_result 同时关联 exam 和 employee，记录每个考生在每次考试中的得分和结果状态。

表间外键关系全部采用 RESTRICT 删除策略（有子记录时禁止删除父记录）和 CASCADE 更新策略（父表主键变更时自动级联更新子表）。这六条外键分别是：training_session.course_id 引用 training_course.id，training_enrollment.session_id 引用 training_session.id，training_enrollment.employee_id 引用 employee.id，exam.session_id 引用 training_session.id，exam_result.exam_id 引用 exam.id，exam_result.employee_id 引用 employee.id。

## 人员信息表 employee

### 表概述

employee 表用于保存参加培训和考试的人员基本信息，是数据库的核心基础表之一。表中保存的是人员的当前最新状态，包括当前部门和当前职位。如果人员发生部门调动或职位变更，本表会更新为最新值，而历史培训记录中的部门和职位信息则通过 training_enrollment 表中的快照字段保存。

### 字段定义

字段名：id
数据类型：BIGINT UNSIGNED
是否允许为空：否（NOT NULL）
键类型：主键（PRIMARY KEY）
默认值：自动递增（AUTO_INCREMENT）
字段说明：人员唯一编号，系统自动分配，作为该表的主键，被 training_enrollment 和 exam_result 表的外键引用。

字段名：employee_no
数据类型：VARCHAR(50)
是否允许为空：否
键类型：唯一键（UNIQUE KEY）
字段说明：工号或学号，每个人员的唯一业务标识，不允许重复。该字段建立了唯一索引 uk_employee_no。

字段名：employee_name
数据类型：VARCHAR(100)
是否允许为空：否
键类型：普通索引
字段说明：人员姓名，不允许为空。建立了普通索引 idx_employee_name 以支持按姓名查询。

字段名：gender
数据类型：VARCHAR(10)
是否允许为空：是
默认值：NULL
字段说明：性别，可选字段。

字段名：department_name
数据类型：VARCHAR(100)
是否允许为空：是
默认值：NULL
键类型：普通索引
字段说明：人员当前所属部门名称。建立了索引 idx_department_name，用于按部门筛选和统计。该字段会随人员调动而更新。

字段名：position_name
数据类型：VARCHAR(100)
是否允许为空：是
默认值：NULL
字段说明：人员当前的职位名称。与 department_name 一样会随调动更新。

字段名：phone
数据类型：VARCHAR(30)
是否允许为空：是
默认值：NULL
字段说明：联系电话。

字段名：email
数据类型：VARCHAR(255)
是否允许为空：是
默认值：NULL
字段说明：邮箱地址。

字段名：employee_status
数据类型：VARCHAR(20)
是否允许为空：否
默认值：在职
键类型：普通索引
字段说明：人员当前的在职状态。允许的取值为：在职（在职人员）、离职（已离职人员）、退休（已退休人员）、实习（实习生）、其他（其他状态）。默认值为"在职"。该字段建立了索引 idx_employee_status，支持按状态筛选。

字段名：hire_date
数据类型：DATE
是否允许为空：是
默认值：NULL
字段说明：入职日期。

字段名：remarks
数据类型：TEXT
是否允许为空：是
默认值：NULL
字段说明：备注信息，用于存储额外的说明文字。

字段名：created_at
数据类型：DATETIME
是否允许为空：否
默认值：CURRENT_TIMESTAMP
字段说明：数据创建时间，由数据库自动填充。

字段名：updated_at
数据类型：DATETIME
是否允许为空：否
默认值：CURRENT_TIMESTAMP ON UPDATE
字段说明：数据最后修改时间，每次更新记录时自动刷新为当前时间。

### 约束和索引汇总

employee 表包含一个主键约束（PRIMARY KEY on id），一个唯一约束 uk_employee_no（确保工号唯一），四个索引：idx_employee_name（按姓名查询）、idx_department_name（按部门查询）、idx_employee_status（按状态查询），以及主键自带的聚集索引。

### 当前测试数据概况

表中存有 15 条人员记录，覆盖五种员工状态：在职 11 人、离职 1 人（王五 EMP003，2024年6月离职）、退休 1 人（杨十六 EMP014，2025年1月退休）、实习 1 人（刘十三 EMP011，2025届实习生）、其他 1 人。涉及五个部门：技术部（含软件工程师、高级软件工程师、架构师、测试工程师、实习生）、人事部（人事专员、人事经理）、财务部（会计、出纳、财务主管）、管理部（部门经理、副总经理）、安全部（安全主管、安全专员）。

## 培训课程表 training_course

### 表概述

training_course 表用于保存培训课程本身的基础信息，是培训体系的顶层实体。培训课程表示的是课程内容定义，而不是某一次具体举办的培训活动。例如"安全生产基础培训"是一门培训课程（course_code 为 SAFE-001），它可以在 2023 年、2024 年、2025 年分别举办具体场次。课程与场次的分离设计使得同一门课程可以跨年度重复使用，课程内容变更不影响历史培训记录。课程停用后（is_active 设为 0），历史培训场次仍然保留。

### 字段定义

字段名：id
数据类型：BIGINT UNSIGNED
是否允许为空：否
键类型：主键
默认值：AUTO_INCREMENT
字段说明：课程唯一编号，被 training_session 表的 course_id 外键引用。

字段名：course_code
数据类型：VARCHAR(50)
是否允许为空：否
键类型：唯一键
字段说明：课程编号，唯一标识一门课程。示例值：SAFE-001 表示安全生产类第1门课，NET-001 表示网络安全类第1门课。建立了唯一索引 uk_course_code。

字段名：course_name
数据类型：VARCHAR(200)
是否允许为空：否
键类型：普通索引
字段说明：课程名称，不允许为空。建立了索引 idx_course_name。

字段名：course_category
数据类型：VARCHAR(100)
是否允许为空：是
默认值：NULL
键类型：普通索引
字段说明：课程所属类别，建立了索引 idx_course_category。当前测试数据涵盖的类别有：安全培训、信息安全、管理培训、技术培训、法律培训、质量培训、健康培训。

字段名：standard_hours
数据类型：DECIMAL(6,2)
是否允许为空：是
默认值：NULL
约束：CHECK 约束 chk_standard_hours，允许为 NULL，但不允许为负数（>= 0）
字段说明：该课程的标准培训学时。例如安全生产基础培训标准学时为 8 学时，Python数据分析进阶为 24 学时。

字段名：course_level
数据类型：VARCHAR(30)
是否允许为空：是
默认值：NULL
字段说明：课程难度等级。允许的取值为：初级（初级课程）、中级（中级课程）、高级（高级课程）、通用（通用课程，无特定等级要求）、其他（其他等级）。

字段名：course_description
数据类型：TEXT
是否允许为空：是
默认值：NULL
字段说明：课程内容详细说明，TEXT 类型支持长文本。

字段名：is_active
数据类型：TINYINT(1)
是否允许为空：否
默认值：1
字段说明：课程启用状态。值为 1 表示课程当前启用中，值为 0 表示课程已停用。停用不影响历史数据。默认新创建的课程为启用状态。

字段名：created_at
数据类型：DATETIME
是否允许为空：否
默认值：CURRENT_TIMESTAMP
字段说明：数据创建时间。

字段名：updated_at
数据类型：DATETIME
是否允许为空：否
默认值：CURRENT_TIMESTAMP ON UPDATE
字段说明：数据最后修改时间，每次更新自动刷新。

### 约束和索引汇总

training_course 表包含一个主键（id），一个唯一约束 uk_course_code（课程编号唯一），两个普通索引 idx_course_name 和 idx_course_category，一个 CHECK 约束 chk_standard_hours（标准学时不能为负数）。

### 当前测试数据概况

表中存有 8 门课程，覆盖六大类别：安全类 3 门（安全生产基础培训 SAFE-001、消防安全培训 FIRE-001、急救知识与技能培训 FIRST-001）、信息安全类 1 门（网络安全意识培训 NET-001）、管理类 1 门（中层管理技能提升 MGMT-001）、技术类 1 门（Python数据分析进阶 TECH-001）、法律类 1 门（劳动法律法规培训 LAW-001）、质量类 1 门（质量管理体系内审员 QUAL-001）。学时范围从 4 到 24 学时，等级涵盖初级、中级、高级和通用。所有课程当前均为启用状态。

## 培训场次表 training_session

### 表概述

training_session 表用于保存某门培训课程在特定年度实际举办的具体培训活动。它是培训课程和年度执行之间的桥梁。例如培训课程"安全生产基础培训"在 2025 年举办的"2025年第一期安全生产基础培训"就是一条培训场次记录。同一门课程在不同年度可以举办多场，同一门课程在同一年度也可以举办多期。培训场次记录包含了完整的时间安排、地点、讲师、费用、参加人数和培训状态等信息。

### 字段定义

字段名：id
数据类型：BIGINT UNSIGNED
是否允许为空：否
键类型：主键
默认值：AUTO_INCREMENT
字段说明：培训场次唯一编号，被 training_enrollment.session_id 和 exam.session_id 引用。

字段名：course_id
数据类型：BIGINT UNSIGNED
是否允许为空：否
键类型：外键 + 普通索引
字段说明：对应的培训课程编号，外键引用 training_course.id。删除策略为 RESTRICT（有场次记录时不允许删除课程），更新策略为 CASCADE（课程 ID 变更时自动更新）。该字段建立了索引 idx_course_id，用于按课程查询所有场次。

字段名：training_year
数据类型：YEAR
是否允许为空：否
键类型：普通索引
字段说明：培训所属年度。建立了索引 idx_training_year，用于按年度筛选培训活动。当前测试数据覆盖 2023、2024、2025 三个年度。

字段名：session_no
数据类型：VARCHAR(50)
是否允许为空：否
键类型：唯一键
字段说明：培训场次编号，唯一标识一场培训。命名规则为 TS-YYYY-NNN，其中 TS 表示场次，YYYY 为年度，NNN 为序号。例如 TS-2025-001 表示 2025 年第 1 号培训场次。建立了唯一索引 uk_session_no。

字段名：session_name
数据类型：VARCHAR(200)
是否允许为空：否
字段说明：培训场次全称，如"2025年第一期安全生产基础培训"。

字段名：start_date
数据类型：DATE
是否允许为空：否
键类型：普通索引
字段说明：培训开始日期。建立了索引 idx_start_date，用于按日期范围查询。

字段名：end_date
数据类型：DATE
是否允许为空：否
约束：CHECK 约束 chk_end_date，要求 end_date >= start_date，即结束日期不能早于开始日期
字段说明：培训结束日期。

字段名：actual_hours
数据类型：DECIMAL(6,2)
是否允许为空：是
默认值：NULL
约束：CHECK 约束 chk_actual_hours，允许为 NULL 但不允许为负数
字段说明：实际培训学时数，可能与课程的标准学时不同。

字段名：trainer_name
数据类型：VARCHAR(100)
是否允许为空：是
默认值：NULL
字段说明：培训讲师姓名。

字段名：organizer
数据类型：VARCHAR(200)
是否允许为空：是
默认值：NULL
字段说明：培训组织单位，如安全管理部、人力资源部、信息技术部。

字段名：training_location
数据类型：VARCHAR(255)
是否允许为空：是
默认值：NULL
字段说明：培训举办地点，如"公司A栋301会议室"、"外部培训中心"。

字段名：training_method
数据类型：VARCHAR(30)
是否允许为空：否
默认值：线下
字段说明：培训方式。允许的取值为：线下（线下面授培训）、线上（通过网络平台进行线上培训）、线上线下结合（混合式培训，部分人员线下参加部分线上参加）、自学（学员自主学习）、其他（其他培训方式）。默认值为线下。

字段名：session_status
数据类型：VARCHAR(30)
是否允许为空：否
默认值：计划中
键类型：普通索引
字段说明：培训场次当前所处的状态。允许的取值为：计划中（培训已列入计划但尚未开始报名）、报名中（正在接受人员报名）、进行中（培训正在举行）、已结束（培训已完成）、已取消（培训因故取消）。建立了索引 idx_session_status。

字段名：planned_count
数据类型：INT UNSIGNED
是否允许为空：是
默认值：NULL
约束：CHECK 约束 chk_planned_count，不允许为负数
字段说明：计划参加培训的人数。

字段名：participant_count
数据类型：INT UNSIGNED
是否允许为空：是
默认值：NULL
约束：CHECK 约束 chk_participant_count，不允许为负数
字段说明：实际参加培训的人数。

字段名：training_fee
数据类型：DECIMAL(10,2)
是否允许为空：是
默认值：NULL
约束：CHECK 约束 chk_training_fee，不允许为负数
字段说明：培训总费用，以元为单位。例如外部培训中心的管理培训费用为 15000 元。

字段名：remarks
数据类型：TEXT
是否允许为空：是
默认值：NULL
字段说明：备注信息。例如"因疫情取消"。

字段名：created_at
数据类型：DATETIME
是否允许为空：否
默认值：CURRENT_TIMESTAMP
字段说明：数据创建时间。

字段名：updated_at
数据类型：DATETIME
是否允许为空：否
默认值：CURRENT_TIMESTAMP ON UPDATE
字段说明：数据最后修改时间。

### 约束和索引汇总

training_session 表包含一个主键，一个唯一约束 uk_session_no，一个外键 fk_session_course（引用 training_course），四个普通索引（idx_course_id、idx_training_year、idx_start_date、idx_session_status），五个 CHECK 约束（chk_end_date 结束日期不早于开始日期、chk_actual_hours 实际学时非负、chk_planned_count 计划人数非负、chk_participant_count 实际人数非负、chk_training_fee 培训费用非负）。

### 当前测试数据概况

表中存有 12 条培训场次记录，分布在三个年度：2025 年 6 场（含进行中和报名中的场次）、2024 年 4 场、2023 年 2 场。培训状态分布为：已结束 7 场、计划中 1 场、报名中 1 场、进行中 1 场、已取消 1 场（2023 年安全生产培训因疫情取消）。培训方式涵盖线下、线上、线上线下结合。费用范围从 1500 元（急救培训）到 15000 元（管理培训）。

## 人员培训记录表 training_enrollment

### 表概述

training_enrollment 表用于保存人员参加培训的具体记录，是连接人员（employee）和培训场次（training_session）的多对多关联表。一个人员可以参加多场不同培训，一场培训可以有多人参加。该表除了记录参训的基本事实外，还记录了出勤情况（出勤学时、出勤率）、培训完成状态、培训评价、证书信息，以及参训时的部门和职位快照。部门快照（department_snapshot）和职位快照（position_snapshot）是两个重要的历史追踪字段——它们记录的是人员参加该场培训时所属的部门和担任的职位，不会随人员在 employee 表中的部门和职位变更而改变。这确保了即使人员后来调岗或离职，仍能准确追溯到其参训时的组织归属。例如王五（EMP003）2023 年参加培训时属于财务部会计，2024 年离职后 employee 表的当前状态已变为"离职"且不再反映部门信息，但培训记录中的快照仍保留"财务部-会计"。

### 字段定义

字段名：id
数据类型：BIGINT UNSIGNED
是否允许为空：否
键类型：主键
默认值：AUTO_INCREMENT
字段说明：培训记录的唯一编号。

字段名：session_id
数据类型：BIGINT UNSIGNED
是否允许为空：否
键类型：外键 + 普通索引
字段说明：培训场次编号，外键引用 training_session.id。是唯一约束 uk_session_employee 的第一个组成部分。

字段名：employee_id
数据类型：BIGINT UNSIGNED
是否允许为空：否
键类型：外键 + 普通索引
字段说明：参加培训的人员编号，外键引用 employee.id。是唯一约束 uk_session_employee 的第二个组成部分。session_id 和 employee_id 的组合唯一，确保同一人不能在同一场培训中出现多条报名记录。

字段名：signup_date
数据类型：DATE
是否允许为空：是
默认值：NULL
字段说明：人员报名参加该场培训的日期。

字段名：attendance_hours
数据类型：DECIMAL(6,2)
是否允许为空：是
默认值：NULL
字段说明：人员实际出勤的学时数。可能与培训场次的总学时不同，例如某场培训总学时 8 小时，某人员只参加了 6 小时。

字段名：attendance_rate
数据类型：DECIMAL(5,2)
是否允许为空：是
默认值：NULL
约束：CHECK 约束 chk_attendance_rate，允许为 NULL，或取值范围在 0.00 到 100.00 之间
字段说明：出勤率，以百分数表示。100.00 表示全勤，0.00 表示完全缺席，80.00 表示出勤率为 80%。该字段为百分数格式而非小数格式。

字段名：completion_status
数据类型：VARCHAR(30)
是否允许为空：否
默认值：已报名
键类型：普通索引
字段说明：培训完成状态。允许的取值为：已报名（已报名但尚未开始或未参加）、培训中（正在参加培训过程中）、已完成（已顺利完成培训）、未完成（参加了部分但未完成全部培训内容）、缺席（报名后完全未参加）、已取消（报名记录因培训取消或其他原因被取消）。默认新记录的值为"已报名"。建立了索引 idx_completion_status。

字段名：completion_date
数据类型：DATE
是否允许为空：是
默认值：NULL
字段说明：培训完成日期，通常与培训结束日期一致。

字段名：evaluation_score
数据类型：DECIMAL(5,2)
是否允许为空：是
默认值：NULL
字段说明：学员对培训的评价分数，用于培训质量反馈。

字段名：evaluation
数据类型：VARCHAR(500)
是否允许为空：是
默认值：NULL
字段说明：学员对培训的文字评价内容，最大 500 字符。

字段名：certificate_no
数据类型：VARCHAR(100)
是否允许为空：是
默认值：NULL
字段说明：培训合格后颁发的证书编号。例如 CERT-2025-001 表示 2025 年颁发的第 1 号证书。

字段名：certificate_date
数据类型：DATE
是否允许为空：是
默认值：NULL
字段说明：证书发放日期。

字段名：department_snapshot
数据类型：VARCHAR(100)
是否允许为空：是
默认值：NULL
键类型：普通索引
字段说明：参加培训时人员所在的部门（历史快照）。此字段在报名时从 employee.department_name 复制快照值，之后不会随 employee 表的更新而改变。建立了索引 idx_department_snapshot，用于按参训时的部门进行历史统计分析。

字段名：position_snapshot
数据类型：VARCHAR(100)
是否允许为空：是
默认值：NULL
字段说明：参加培训时人员的职位（历史快照）。与 department_snapshot 配合使用，完整记录参训时的组织信息。

字段名：remarks
数据类型：TEXT
是否允许为空：是
默认值：NULL
字段说明：备注信息，如"有半天请假"、"中途因紧急任务离场"。

字段名：created_at
数据类型：DATETIME
是否允许为空：否
默认值：CURRENT_TIMESTAMP
字段说明：数据创建时间。

字段名：updated_at
数据类型：DATETIME
是否允许为空：否
默认值：CURRENT_TIMESTAMP ON UPDATE
字段说明：数据最后修改时间。

### 约束和索引汇总

training_enrollment 表包含一个主键，一个复合唯一约束 uk_session_employee（session_id + employee_id 组合唯一，防止同一人重复参加同一场培训），两条外键（fk_enrollment_session 引用 training_session、fk_enrollment_employee 引用 employee），四个普通索引（idx_session_id、idx_employee_id、idx_completion_status、idx_department_snapshot），一个 CHECK 约束 chk_attendance_rate（出勤率范围 0.00 到 100.00）。

### 当前测试数据概况

表中存有 38 条培训记录，完成状态分布为：已完成 26 条、培训中 4 条（Python培训进行中的4名学员）、未完成 2 条、缺席 1 条（黄十四因出差缺席）、已报名 1 条（实习期间报名未参加）、已取消 2 条（因疫情取消的培训报名）。证书发放共计 28 条，证书记录按发放日期分布从 2023 年到 2025 年。出勤率涵盖 100%（全勤）、75%（部分出勤）、50%（一半出勤）、0%（缺席）等多种情况。

## 考试信息表 exam

### 表概述

exam 表用于保存培训对应的考试安排信息。一场培训可以没有考试，也可以安排多次不同类型的考试。例如一场安全生产培训可能安排结业考试和补考两场考试，一场质量体系培训可能安排认证考试。考试信息包括考试的类型、时间、时长、总分、及格线、考试方式、地点和状态等。考试与培训场次之间是一对多关系（一场培训可有多次考试），通过 session_id 外键关联。

### 字段定义

字段名：id
数据类型：BIGINT UNSIGNED
是否允许为空：否
键类型：主键
默认值：AUTO_INCREMENT
字段说明：考试唯一编号，被 exam_result.exam_id 引用。

字段名：session_id
数据类型：BIGINT UNSIGNED
是否允许为空：否
键类型：外键 + 普通索引
字段说明：对应的培训场次编号，外键引用 training_session.id。建立了索引 idx_session_id。

字段名：exam_no
数据类型：VARCHAR(50)
是否允许为空：否
键类型：唯一键
字段说明：考试编号，唯一标识一次考试。命名规则为 EX-YYYY-NNN。建立了唯一索引 uk_exam_no。

字段名：exam_name
数据类型：VARCHAR(200)
是否允许为空：否
字段说明：考试名称，如"2025年安全生产培训结业考试"。

字段名：exam_type
数据类型：VARCHAR(30)
是否允许为空：否
默认值：结业考试
键类型：普通索引
字段说明：考试类型。允许的取值为：结业考试（培训结束后的正式考试）、补考（为不合格或未参加人员安排的再次考试）、认证考试（需要取得资格认证的考试，如内审员认证）、阶段考试（培训过程中的阶段性测验）、入学考试（培训开始前的摸底或选拔考试）、其他（其他考试类型）。建立了索引 idx_exam_type。

字段名：exam_date
数据类型：DATETIME
是否允许为空：否
键类型：普通索引
字段说明：考试的具体日期和时间。建立了索引 idx_exam_date，用于按时间查询考试安排。

字段名：duration_minutes
数据类型：INT UNSIGNED
是否允许为空：是
默认值：NULL
约束：CHECK 约束 chk_duration_minutes，不允许为负数
字段说明：考试时长，以分钟为单位。例如安全生产结业考试为 90 分钟，认证考试为 150 分钟。

字段名：total_score
数据类型：DECIMAL(6,2)
是否允许为空：否
默认值：100.00
约束：CHECK 约束 chk_total_score，要求总分必须大于 0
字段说明：考试的总分。大多数考试总分为 100 分，个别认证考试可能为 120 分（如质量管理体系内审员认证考试总分为 120 分）。默认值为 100 分。

字段名：pass_score
数据类型：DECIMAL(6,2)
是否允许为空：否
默认值：60.00
约束：CHECK 约束 chk_pass_score，要求及格分数 >= 0 且 <= total_score
字段说明：考试的及格分数线。默认值为 60 分（即总分 100 分的情况下 60 分及格）。管理类考试可能设为 70 分，认证考试可能设为总分的 70%（如 120 分总分及格线为 84 分）。

字段名：exam_method
数据类型：VARCHAR(30)
是否允许为空：否
默认值：闭卷
字段说明：考试方式。允许的取值为：闭卷（闭卷笔试，不允许查阅资料）、开卷（开卷笔试，允许查阅指定资料）、机考（上机考试，全程在计算机上完成）、实操（实际操作考试，考察动手能力）、线上（通过在线平台进行的远程考试）、面试（口试或面试形式的考试）、其他（其他考试方式）。

字段名：exam_location
数据类型：VARCHAR(255)
是否允许为空：是
默认值：NULL
字段说明：考试地点，如"公司A栋301会议室"、"外部培训中心"。

字段名：exam_status
数据类型：VARCHAR(30)
是否允许为空：否
默认值：未开始
键类型：普通索引
字段说明：考试当前状态。允许的取值为：未开始（考试尚未开始）、进行中（考试正在进行）、已结束（考试已完成，成绩可录入）、已取消（考试因故取消）。建立了索引 idx_exam_status。

字段名：paper_count
数据类型：INT UNSIGNED
是否允许为空：是
默认值：NULL
约束：CHECK 约束 chk_paper_count，不允许为负数
字段说明：本次考试的试卷印刷或准备数量。

字段名：invigilator
数据类型：VARCHAR(100)
是否允许为空：是
默认值：NULL
字段说明：监考人员姓名，可以是多人，用逗号分隔。

字段名：remarks
数据类型：TEXT
是否允许为空：是
默认值：NULL
字段说明：备注信息。

字段名：created_at
数据类型：DATETIME
是否允许为空：否
默认值：CURRENT_TIMESTAMP
字段说明：数据创建时间。

字段名：updated_at
数据类型：DATETIME
是否允许为空：否
默认值：CURRENT_TIMESTAMP ON UPDATE
字段说明：数据最后修改时间。

### 约束和索引汇总

exam 表包含一个主键，一个唯一约束 uk_exam_no，一条外键 fk_exam_session（引用 training_session），四个普通索引（idx_session_id、idx_exam_date、idx_exam_type、idx_exam_status），四个 CHECK 约束（chk_duration_minutes 时长非负、chk_total_score 总分大于零、chk_pass_score 及格线非负且不超过总分、chk_paper_count 试卷数量非负）。

### 当前测试数据概况

表中存有 8 场考试记录，考试类型分布为：结业考试 5 场、补考 1 场（安全生产培训补考）、认证考试 1 场（质量管理体系内审员认证）、阶段考试 1 场（Python培训阶段测验）。考试状态分布为：已结束 7 场、未开始 1 场（2025年7月的内审员认证考试尚未开始）。考试总分有 100 分（常规）、80 分（消防安全考试）、120 分（认证考试）三种。及格线分别有 48 分（对应 80 分总分的 60%）、60 分（100 分总分的 60%）、70 分（管理考试的较高要求）、84 分（120 分总分的 70%）。考试方式包括闭卷、机考、开卷。时长从 45 分钟到 150 分钟不等。

## 考试成绩表 exam_result

### 表概述

exam_result 表用于保存每个人员参加每次考试后的成绩和结果。一次考试可以有多人参加，一个人员也可以多次参加同一场考试（如首考和补考），通过 attempt_no 字段区分第几次考试。is_best_result 字段用于标记该人员在该考试中的最佳成绩记录。当同一人参加了同一考试多次（如首考 86 分、补考 94 分），只有最高成绩对应的那条记录的 is_best_result 设为 1。该表与 exam 表和 employee 表关联，构成考试成绩的完整记录。

### 字段定义

字段名：id
数据类型：BIGINT UNSIGNED
是否允许为空：否
键类型：主键
默认值：AUTO_INCREMENT
字段说明：成绩记录的唯一编号。

字段名：exam_id
数据类型：BIGINT UNSIGNED
是否允许为空：否
键类型：外键 + 普通索引
字段说明：考试编号，外键引用 exam.id。是唯一约束 uk_exam_employee_attempt 的第一个组成部分。

字段名：employee_id
数据类型：BIGINT UNSIGNED
是否允许为空：否
键类型：外键 + 普通索引
字段说明：参加考试的人员编号，外键引用 employee.id。是唯一约束 uk_exam_employee_attempt 的第二个组成部分。

字段名：attempt_no
数据类型：INT UNSIGNED
是否允许为空：否
默认值：1
约束：CHECK 约束 chk_attempt_no，要求 >= 1
字段说明：该人员第几次参加该场考试。首次参加默认为 1，补考时递增为 2 或更大。是唯一约束 uk_exam_employee_attempt 的第三个组成部分。exam_id、employee_id 和 attempt_no 三个字段的组合唯一，确保同一人在同一考试的同一次尝试只有一条记录。

字段名：score
数据类型：DECIMAL(6,2)
是否允许为空：是
默认值：NULL
键类型：普通索引
约束：CHECK 约束 chk_score，允许为 NULL 但不允许为负数
字段说明：考试得分。如果 result_status 为缺考，score 通常为 NULL；如果 result_status 为合格或不合格，score 应有具体数值。建立了索引 idx_score 用于按分数排序和范围查询。

字段名：result_status
数据类型：VARCHAR(30)
是否允许为空：否
默认值：未考试
键类型：普通索引
字段说明：考试结果状态。允许的取值为：未考试（尚未参加考试，默认状态）、考试中（正在考试过程中）、合格（成绩达到或超过及格线）、不合格（成绩低于及格线）、缺考（报名但未到场参加考试）、作弊（被发现考试作弊行为）、成绩作废（成绩因违规或其他原因被作废）。建立了索引 idx_result_status。

字段名：answer_sheet_no
数据类型：VARCHAR(100)
是否允许为空：是
默认值：NULL
字段说明：答题卡编号。

字段名：paper_no
数据类型：VARCHAR(100)
是否允许为空：是
默认值：NULL
字段说明：试卷编号，如 PAPER-A-001。

字段名：seat_no
数据类型：VARCHAR(30)
是否允许为空：是
默认值：NULL
字段说明：考生在考场的座位号。

字段名：absent_reason
数据类型：VARCHAR(500)
是否允许为空：是
默认值：NULL
字段说明：缺考原因说明。仅当 result_status 为缺考时填写，如"出差未归"。

字段名：graded_at
数据类型：DATETIME
是否允许为空：是
默认值：NULL
字段说明：阅卷完成并登记成绩的时间。

字段名：grader_name
数据类型：VARCHAR(100)
是否允许为空：是
默认值：NULL
字段说明：阅卷老师或成绩登记人员的姓名。

字段名：is_best_result
数据类型：TINYINT(1)
是否允许为空：否
默认值：0
键类型：普通索引
字段说明：该条成绩记录是否为该人员在该考试中的最佳成绩。值为 1 表示是最佳成绩（在多次考试中得分最高），值为 0 表示不是最佳成绩。同一人在同一考试中仅有一条记录应标记为 1。建立了索引 idx_is_best_result。

字段名：remarks
数据类型：TEXT
是否允许为空：是
默认值：NULL
字段说明：备注信息，如"补考通过"、"考试中携带禁止资料，成绩作废"。

字段名：created_at
数据类型：DATETIME
是否允许为空：否
默认值：CURRENT_TIMESTAMP
字段说明：数据创建时间。

字段名：updated_at
数据类型：DATETIME
是否允许为空：否
默认值：CURRENT_TIMESTAMP ON UPDATE
字段说明：数据最后修改时间。

### 业务规则

考试成绩表有以下重要的业务规则需要关注。第一，缺考规则：如果 result_status 为"缺考"，则 score 字段通常应为 NULL，且 absent_reason 字段应有相关说明。第二，成绩规则：如果 result_status 为"合格"或"不合格"，则 score 必须有具体的数值且应在 0 到考试总分的范围内。第三，最佳成绩规则：同一人在同一考试中如果有多条记录（如首考和补考），只有得分最高的一条记录的 is_best_result 应设为 1，其余设为 0。第四，attempt_no 规则：首次参加考试的 attempt_no 为 1，每次补考依次递增。第五，作弊或成绩作废规则：如果发现作弊或成绩作废，result_status 分别设为"作弊"或"成绩作废"，score 可以被保留为原始分数但状态说明成绩无效。

### 约束和索引汇总

exam_result 表包含一个主键，一个复合唯一约束 uk_exam_employee_attempt（exam_id + employee_id + attempt_no 组合唯一），两条外键（fk_result_exam 引用 exam、fk_result_employee 引用 employee），五个普通索引（idx_exam_id、idx_employee_id、idx_result_status、idx_score、idx_is_best_result），两个 CHECK 约束（chk_attempt_no 考试次数 >= 1、chk_score 成绩非负）。

### 当前测试数据概况

表中存有 27 条考试成绩记录。考试结果分布为：合格 17 条、不合格 4 条、缺考 1 条（黄十四因出差缺考网络安全考试，score 为 NULL）、作弊 1 条（刘十三在劳动法考试中携带禁止资料，成绩作废）、未考试 4 条（对应尚未参加考试或培训进行中的情况）。补考记录有 2 条：李四（EMP002）安全生产首考 55 分不合格（attempt_no=1），补考 78 分合格（attempt_no=2），其 is_best_result 在补考后设为 1；张三（EMP001）安全生产首考 86 分已合格（attempt_no=1，is_best_result 后设为 0），自愿参加补考获得 94 分（attempt_no=2，is_best_result 设为 1）。最佳成绩标记正确反映了每次考试的最高分。分数范围从 45 分（不合格的最低分）到 96 分（周八在网络安全考试中的最高分）。

## 数据库约束命名规范

数据库中的约束和索引遵循统一的命名规范，便于识别类型和用途。主键统一命名为 PRIMARY。唯一约束以 uk_ 为前缀，后接字段名，如 uk_employee_no 表示 employee_no 字段上的唯一约束。普通索引以 idx_ 为前缀，后接字段名，如 idx_employee_name。外键约束以 fk_ 为前缀，后接关联关系，如 fk_session_course 表示 session 表的 course_id 引用 course 表。CHECK 约束以 chk_ 为前缀，后接校验字段，如 chk_end_date。所有索引名称均为小写字母加下划线，不与 MySQL 保留字冲突。

## 完整外键关系详解

数据库的六条外键关系构成了完整的数据引用完整性保障体系。每条外键的删除策略均为 RESTRICT，意味着当父表记录被删除时，如果子表中存在引用该记录的关联数据，则删除操作会被拒绝。这确保了数据的完整性——例如无法删除一门培训课程如果它已经有了培训场次记录，无法删除一个人员如果他有培训记录或考试成绩。更新策略均为 CASCADE，意味着当父表的主键值发生变更时，子表中外键字段的值会自动同步更新。这在极少情况下（如主键重新编号）能保持引用一致性。

外键关系一：training_session.course_id 引用 training_course.id。约束名 fk_session_course。这确保了每个培训场次必须属于一门已存在的培训课程。当查询"某门课程的所有培训场次"时，通过此关联进行 JOIN。

外键关系二：training_enrollment.session_id 引用 training_session.id。约束名 fk_enrollment_session。这确保了每条参训记录必须关联到一个有效的培训场次。

外键关系三：training_enrollment.employee_id 引用 employee.id。约束名 fk_enrollment_employee。这确保了每条参训记录必须关联到一位有效的人员。

外键关系四：exam.session_id 引用 training_session.id。约束名 fk_exam_session。这确保了每次考试必须属于一场有效的培训。

外键关系五：exam_result.exam_id 引用 exam.id。约束名 fk_result_exam。这确保了每条成绩记录必须关联到一次有效的考试。

外键关系六：exam_result.employee_id 引用 employee.id。约束名 fk_result_employee。这确保了每条成绩记录必须关联到一位有效的人员。

## 完整 CHECK 约束详解

数据库使用了 14 条 CHECK 约束来在数据库层面保障数据质量，所有约束均在 MySQL 8.0 及以上版本中生效。这些约束分为数值范围约束和日期逻辑约束两大类。

数值范围约束方面：training_course 表的 chk_standard_hours 确保标准学时要么为空要么非负。training_session 表有四个数值约束：chk_actual_hours（实际学时非负）、chk_planned_count（计划人数非负）、chk_participant_count（实际参加人数非负）、chk_training_fee（培训费用非负）。training_enrollment 表的 chk_attendance_rate 确保出勤率在 0.00 到 100.00 之间，即合理百分数范围。exam 表有三个数值约束：chk_duration_minutes（考试时长非负）、chk_total_score（总分必须大于 0）、chk_paper_count（试卷数量非负）。exam_result 表有两个数值约束：chk_attempt_no（考试次数至少为 1）、chk_score（成绩要么为空要么非负）。

日期逻辑约束方面：training_session 表的 chk_end_date 确保培训结束日期不早于开始日期，防止录入错误的时间范围。

分值逻辑约束方面：exam 表的 chk_pass_score 确保及格分数线不小于 0 且不超过考试总分，防止设置不合理的及格线（如总分 100 分但及格线设为 120 分）。

## 测试数据覆盖场景

数据库的测试数据覆盖了 18 个业务场景，每个场景验证了特定的查询或数据模式。

场景一，按年度查询培训：可查询 2025 年举办的 6 场培训，结果按开始日期排序。场景二，查询课程历年的培训场次：安全生产培训（SAFE-001）在 2023、2024、2025 三个年度共举办了 3 场培训，其中 2023 年场次状态为"已取消"。场景三，人员培训履历查询：张三（EMP001）共参加了 8 场培训，状态包括培训中、已完成和已取消，完整覆盖各种完成状态。场景四，部门培训统计：技术部在 2023 至 2025 年间共有 8 次培训报名记录，按年度和课程分组统计完成和未完成情况。场景五，培训参加人员名单：2025 年安全生产培训（TS-2025-001）有 5 人报名参加。场景六，出勤率统计：同一场培训的出勤率从 50% 到 100% 不等，平均出勤率 85%。

场景七，考试安排查询：2025 年安全生产培训安排了两场考试——结业考试和补考，体现了"一场培训多次考试"的模式。场景八，考试成绩查询：结业考试中最高分 92 分（赵六），最低分 55 分（李四），4 人合格 1 人不合格。场景九，合格不合格统计：直接按 result_status 汇总得出各状态人数。场景十，年度考试通过率：2025 年度各场考试的通过率从 66.67%（管理考试）到 100%（补考）不等，补考因为只有准备充分的考生参加所以通过率最高。场景十一，补考追踪：李四首考 55 分不合格，参加补考获得 78 分合格；张三首考 86 分已合格，自愿补考获得 94 分提高成绩。这两个案例展示了补考的两种动机。场景十二，最佳成绩标记：is_best_result 字段准确反映每人每场考试的最高分记录。

场景十三，缺考人员查询：黄十四（EMP012）因出差未归而缺考网络安全考试，result_status 为"缺考"，score 为 NULL，absent_reason 记录了缺考原因。场景十四，历史快照验证：王五（EMP003）在 2023 年参训时属于财务部会计，2024 年 6 月离职，employee 表中已显示"离职"，但其在 training_enrollment 表中的 department_snapshot 和 position_snapshot 仍正确保留为"财务部-会计"。场景十五，课程跨年度举办：安全生产基础培训在 2023、2024、2025 三年分别有场次记录，验证了课程与场次的一对多跨年度设计。场景十六，培训和考试取消：2023 年安全生产培训（TS-2023-002）因疫情取消，session_status 为"已取消"，关联的报名记录状态也同步更新为"已取消"。

场景十七，同一人员多次考试：李四和张三在安全生产考试中各有 2 次考试记录，attempt_no 分别为 1 和 2。场景十八，证书管理：共有 28 条证书记录，涵盖不同年度和不同课程的培训证书，证书编号和发放日期均被记录。

## 常用查询 SQL 参考

以下 SQL 查询覆盖了最常见的业务查询需求，可以直接在数据库上执行。

按年度查询全部培训场次：
SELECT s.session_no, s.session_name, c.course_name, s.start_date, s.end_date, s.session_status FROM training_session s JOIN training_course c ON s.course_id = c.id WHERE s.training_year = 2025 ORDER BY s.start_date;

查询某人的全部培训历史：
SELECT e.employee_name, s.training_year, s.session_name, c.course_name, en.attendance_hours, en.attendance_rate, en.completion_status FROM training_enrollment en JOIN employee e ON en.employee_id = e.id JOIN training_session s ON en.session_id = s.id JOIN training_course c ON s.course_id = c.id WHERE e.employee_no = 'EMP001' ORDER BY s.training_year DESC;

查询某部门历年培训完成情况统计：
SELECT e.department_name, s.training_year, c.course_name, COUNT(*) AS total, SUM(CASE WHEN en.completion_status = '已完成' THEN 1 ELSE 0 END) AS completed FROM training_enrollment en JOIN employee e ON en.employee_id = e.id JOIN training_session s ON en.session_id = s.id JOIN training_course c ON s.course_id = c.id WHERE e.department_name = '技术部' GROUP BY e.department_name, s.training_year, c.course_name ORDER BY s.training_year DESC;

统计某年度各场考试的通过率：
SELECT ex.exam_name, COUNT(*) AS total, SUM(CASE WHEN er.result_status = '合格' THEN 1 ELSE 0 END) AS passed, ROUND(SUM(CASE WHEN er.result_status = '合格' THEN 1 ELSE 0 END) / COUNT(*) * 100, 2) AS pass_rate FROM exam_result er JOIN exam ex ON er.exam_id = ex.id JOIN training_session s ON ex.session_id = s.id WHERE s.training_year = 2025 AND er.result_status IN ('合格', '不合格') GROUP BY ex.exam_name ORDER BY pass_rate DESC;

查询补考记录（attempt_no 大于 1）：
SELECT emp.employee_name, ex.exam_name, er.attempt_no, er.score, er.result_status FROM exam_result er JOIN employee emp ON er.employee_id = emp.id JOIN exam ex ON er.exam_id = ex.id WHERE er.attempt_no > 1 ORDER BY emp.employee_no;

查询缺考人员及原因：
SELECT emp.employee_no, emp.employee_name, ex.exam_name, er.absent_reason FROM exam_result er JOIN employee emp ON er.employee_id = emp.id JOIN exam ex ON er.exam_id = ex.id WHERE er.result_status = '缺考';

查询某人历次考试的最佳成绩（is_best_result = 1）：
SELECT emp.employee_no, emp.employee_name, ex.exam_name, er.attempt_no, er.score FROM exam_result er JOIN employee emp ON er.employee_id = emp.id JOIN exam ex ON er.exam_id = ex.id WHERE er.is_best_result = 1 ORDER BY emp.employee_no;

查询持有培训证书的人员及证书信息：
SELECT emp.employee_no, emp.employee_name, c.course_name, en.certificate_no, en.certificate_date FROM training_enrollment en JOIN employee emp ON en.employee_id = emp.id JOIN training_session s ON en.session_id = s.id JOIN training_course c ON s.course_id = c.id WHERE en.certificate_no IS NOT NULL ORDER BY en.certificate_date DESC;

查询特定培训场次的参加人员名单和出勤情况：
SELECT e.employee_no, e.employee_name, e.department_name, en.attendance_hours, en.attendance_rate, en.completion_status FROM training_enrollment en JOIN employee e ON en.employee_id = e.id WHERE en.session_id = 1 ORDER BY e.employee_no;

查询培训场次对应的考试安排：
SELECT e.exam_no, e.exam_name, e.exam_type, e.exam_date, e.total_score, e.pass_score, e.exam_method, e.exam_status FROM exam e WHERE e.session_id = 1 ORDER BY e.exam_date;
