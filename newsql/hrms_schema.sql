-- ============================================================
-- HRMS (Human Resource Management System) — 达梦数据库 Schema
-- 表: companies, departments, employees, positions,
--      salary_records, attendance_records, leave_requests,
--      trainings, employee_trainings, performance_reviews
-- 适配: 达梦数据库 (Dameng DM8)
-- ============================================================

-- 创建表空间 (可选，使用默认表空间 MAIN)
-- CREATE TABLESPACE hrms_ts DATAFILE 'hrms.dbf' SIZE 128;

-- ============================================================
-- 1. companies — 公司信息表
-- ============================================================
CREATE TABLE companies (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    name            VARCHAR2(200)   NOT NULL,
    short_name      VARCHAR2(60)    DEFAULT NULL,
    credit_code     VARCHAR2(30)    NOT NULL,
    legal_person    VARCHAR2(60)    DEFAULT NULL,
    registered_capital NUMBER(16,2) DEFAULT NULL,
    establish_date  DATE            DEFAULT NULL,
    address         VARCHAR2(300)   DEFAULT NULL,
    phone           VARCHAR2(30)    DEFAULT NULL,
    industry        VARCHAR2(60)    DEFAULT NULL,
    employee_count  INT             DEFAULT 0,
    status          VARCHAR2(20)    DEFAULT '正常' CHECK (status IN ('正常','注销','吊销','停业')),
    created_at      TIMESTAMP       DEFAULT SYSDATE,
    updated_at      TIMESTAMP       DEFAULT SYSDATE
);

COMMENT ON TABLE companies IS '公司信息表';
COMMENT ON COLUMN companies.name IS '公司全称';
COMMENT ON COLUMN companies.short_name IS '公司简称';
COMMENT ON COLUMN companies.credit_code IS '统一社会信用代码';
COMMENT ON COLUMN companies.legal_person IS '法定代表人';
COMMENT ON COLUMN companies.registered_capital IS '注册资本(万元)';
COMMENT ON COLUMN companies.establish_date IS '成立日期';
COMMENT ON COLUMN companies.industry IS '所属行业';
COMMENT ON COLUMN companies.employee_count IS '员工人数';
COMMENT ON COLUMN companies.status IS '公司状态: 正常/注销/吊销/停业';

-- ============================================================
-- 2. departments — 部门表
-- ============================================================
CREATE TABLE departments (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    company_id      INT             NOT NULL,
    name            VARCHAR2(80)    NOT NULL,
    parent_id       INT             DEFAULT NULL,
    manager_id      INT             DEFAULT NULL,
    phone           VARCHAR2(30)    DEFAULT NULL,
    floor           INT             DEFAULT NULL,
    budget          NUMBER(14,2)   DEFAULT 0.00,
    description     VARCHAR2(500)   DEFAULT NULL,
    sort_order      INT             DEFAULT 0,
    status          VARCHAR2(20)    DEFAULT '启用' CHECK (status IN ('启用','停用','撤销')),
    created_at      TIMESTAMP       DEFAULT SYSDATE,
    FOREIGN KEY (company_id) REFERENCES companies(id),
    FOREIGN KEY (parent_id) REFERENCES departments(id)
);

COMMENT ON TABLE departments IS '部门表';
COMMENT ON COLUMN departments.company_id IS '所属公司ID';
COMMENT ON COLUMN departments.name IS '部门名称';
COMMENT ON COLUMN departments.parent_id IS '上级部门ID(自关联)';
COMMENT ON COLUMN departments.manager_id IS '部门负责人ID';
COMMENT ON COLUMN departments.budget IS '部门预算(元)';
COMMENT ON COLUMN departments.status IS '部门状态: 启用/停用/撤销';

CREATE INDEX idx_dept_company ON departments(company_id);
CREATE INDEX idx_dept_parent ON departments(parent_id);

-- ============================================================
-- 3. positions — 职位表
-- ============================================================
CREATE TABLE positions (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    name            VARCHAR2(60)    NOT NULL,
    department_id   INT             DEFAULT NULL,
    rank            VARCHAR2(20)    NOT NULL DEFAULT '职员' CHECK (rank IN ('实习生','职员','主管','经理','总监','副总裁','总裁')),
    min_salary      NUMBER(10,2)   DEFAULT NULL,
    max_salary      NUMBER(10,2)   DEFAULT NULL,
    responsibilities CLOB           DEFAULT NULL,
    requirements    CLOB            DEFAULT NULL,
    vacancy_count   INT             DEFAULT 0,
    status          VARCHAR2(20)    DEFAULT '启用' CHECK (status IN ('启用','停用','已取消')),
    created_at      TIMESTAMP       DEFAULT SYSDATE,
    FOREIGN KEY (department_id) REFERENCES departments(id)
);

COMMENT ON TABLE positions IS '职位表';
COMMENT ON COLUMN positions.name IS '职位名称';
COMMENT ON COLUMN positions.rank IS '职位级别';
COMMENT ON COLUMN positions.min_salary IS '最低薪资(元/月)';
COMMENT ON COLUMN positions.max_salary IS '最高薪资(元/月)';
COMMENT ON COLUMN positions.responsibilities IS '岗位职责';
COMMENT ON COLUMN positions.requirements IS '任职要求';
COMMENT ON COLUMN positions.vacancy_count IS '空缺人数';

CREATE INDEX idx_pos_dept ON positions(department_id);
CREATE INDEX idx_pos_rank ON positions(rank);

-- ============================================================
-- 4. employees — 员工表
-- ============================================================
CREATE TABLE employees (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    employee_no     VARCHAR2(20)    NOT NULL,
    name            VARCHAR2(80)    NOT NULL,
    gender          CHAR(1)         NOT NULL CHECK (gender IN ('M','F')),
    birth_date      DATE            DEFAULT NULL,
    id_card         VARCHAR2(18)    DEFAULT NULL,
    phone           VARCHAR2(20)    DEFAULT NULL,
    email           VARCHAR2(120)   DEFAULT NULL,
    native_place    VARCHAR2(100)   DEFAULT NULL,
    education       VARCHAR2(20)    DEFAULT NULL CHECK (education IN ('高中及以下','大专','本科','硕士','博士')),
    major           VARCHAR2(80)    DEFAULT NULL,
    school          VARCHAR2(120)   DEFAULT NULL,
    company_id      INT             NOT NULL,
    department_id   INT             NOT NULL,
    position_id     INT             DEFAULT NULL,
    hire_date       DATE            NOT NULL,
    contract_end    DATE            DEFAULT NULL,
    work_status     VARCHAR2(20)    NOT NULL DEFAULT '在职' CHECK (work_status IN ('在职','试用期','离职','停薪留职','退休')),
    marital_status  VARCHAR2(10)    DEFAULT '未婚' CHECK (marital_status IN ('未婚','已婚','离异','丧偶')),
    home_address    VARCHAR2(300)   DEFAULT NULL,
    emergency_contact VARCHAR2(80)  DEFAULT NULL,
    emergency_phone VARCHAR2(20)    DEFAULT NULL,
    bank_name       VARCHAR2(100)   DEFAULT NULL,
    bank_account    VARCHAR2(30)    DEFAULT NULL,
    social_insurance VARCHAR2(30)   DEFAULT NULL,
    created_at      TIMESTAMP       DEFAULT SYSDATE,
    updated_at      TIMESTAMP       DEFAULT SYSDATE,
    FOREIGN KEY (company_id) REFERENCES companies(id),
    FOREIGN KEY (department_id) REFERENCES departments(id),
    FOREIGN KEY (position_id) REFERENCES positions(id)
);

COMMENT ON TABLE employees IS '员工信息表';
COMMENT ON COLUMN employees.employee_no IS '员工工号';
COMMENT ON COLUMN employees.name IS '员工姓名';
COMMENT ON COLUMN employees.gender IS '性别: M-男, F-女';
COMMENT ON COLUMN employees.id_card IS '身份证号';
COMMENT ON COLUMN employees.native_place IS '籍贯';
COMMENT ON COLUMN employees.education IS '学历';
COMMENT ON COLUMN employees.work_status IS '在职状态';
COMMENT ON COLUMN employees.marital_status IS '婚姻状况';
COMMENT ON COLUMN employees.emergency_contact IS '紧急联系人';
COMMENT ON COLUMN employees.social_insurance IS '社保账号';

CREATE UNIQUE INDEX idx_emp_no ON employees(employee_no);
CREATE UNIQUE INDEX idx_emp_idcard ON employees(id_card);
CREATE INDEX idx_emp_company ON employees(company_id);
CREATE INDEX idx_emp_dept ON employees(department_id);
CREATE INDEX idx_emp_pos ON employees(position_id);
CREATE INDEX idx_emp_status ON employees(work_status);
CREATE INDEX idx_emp_name ON employees(name);
CREATE INDEX idx_emp_phone ON employees(phone);

-- ============================================================
-- 5. salary_records — 薪资记录表
-- ============================================================
CREATE TABLE salary_records (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    employee_id     INT             NOT NULL,
    salary_year     INT             NOT NULL,
    salary_month    INT             NOT NULL CHECK (salary_month >= 1 AND salary_month <= 12),
    base_salary     NUMBER(10,2)   NOT NULL DEFAULT 0.00,
    position_salary NUMBER(10,2)   NOT NULL DEFAULT 0.00,
    performance_bonus NUMBER(10,2) NOT NULL DEFAULT 0.00,
    overtime_pay    NUMBER(10,2)   NOT NULL DEFAULT 0.00,
    subsidy         NUMBER(10,2)   NOT NULL DEFAULT 0.00,
    gross_salary    NUMBER(10,2)   NOT NULL DEFAULT 0.00,
    social_insurance_deduct NUMBER(8,2) DEFAULT 0.00,
    housing_fund_deduct     NUMBER(8,2) DEFAULT 0.00,
    tax_deduct      NUMBER(8,2)    DEFAULT 0.00,
    other_deduct    NUMBER(8,2)    DEFAULT 0.00,
    net_salary      NUMBER(10,2)   NOT NULL DEFAULT 0.00,
    pay_status      VARCHAR2(20)   NOT NULL DEFAULT '待发放' CHECK (pay_status IN ('待发放','已发放','发放失败','已撤回')),
    paid_at         TIMESTAMP      DEFAULT NULL,
    remarks         VARCHAR2(500)  DEFAULT NULL,
    created_at      TIMESTAMP      DEFAULT SYSDATE,
    FOREIGN KEY (employee_id) REFERENCES employees(id),
    UNIQUE (employee_id, salary_year, salary_month)
);

COMMENT ON TABLE salary_records IS '薪资记录表';
COMMENT ON COLUMN salary_records.salary_year IS '薪资年份';
COMMENT ON COLUMN salary_records.salary_month IS '薪资月份';
COMMENT ON COLUMN salary_records.base_salary IS '基本工资';
COMMENT ON COLUMN salary_records.position_salary IS '岗位工资';
COMMENT ON COLUMN salary_records.performance_bonus IS '绩效奖金';
COMMENT ON COLUMN salary_records.overtime_pay IS '加班费';
COMMENT ON COLUMN salary_records.subsidy IS '补贴';
COMMENT ON COLUMN salary_records.gross_salary IS '应发工资';
COMMENT ON COLUMN salary_records.social_insurance_deduct IS '社保扣款';
COMMENT ON COLUMN salary_records.housing_fund_deduct IS '公积金扣款';
COMMENT ON COLUMN salary_records.tax_deduct IS '个税扣款';
COMMENT ON COLUMN salary_records.net_salary IS '实发工资';
COMMENT ON COLUMN salary_records.pay_status IS '发放状态';

CREATE INDEX idx_sal_employee ON salary_records(employee_id);
CREATE INDEX idx_sal_period ON salary_records(salary_year, salary_month);
CREATE INDEX idx_sal_status ON salary_records(pay_status);

-- ============================================================
-- 6. attendance_records — 考勤记录表
-- ============================================================
CREATE TABLE attendance_records (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    employee_id     INT             NOT NULL,
    attend_date     DATE            NOT NULL,
    clock_in        TIMESTAMP       DEFAULT NULL,
    clock_out       TIMESTAMP       DEFAULT NULL,
    status          VARCHAR2(20)    NOT NULL DEFAULT '正常' CHECK (status IN ('正常','迟到','早退','旷工','加班','出差','请假','外勤')),
    late_minutes    INT             DEFAULT 0,
    early_minutes   INT             DEFAULT 0,
    overtime_hours  NUMBER(4,1)    DEFAULT 0.0,
    source          VARCHAR2(20)    DEFAULT '打卡机' CHECK (source IN ('打卡机','手机APP','补卡','外勤打卡')),
    remark          VARCHAR2(300)   DEFAULT NULL,
    created_at      TIMESTAMP       DEFAULT SYSDATE,
    FOREIGN KEY (employee_id) REFERENCES employees(id),
    UNIQUE (employee_id, attend_date)
);

COMMENT ON TABLE attendance_records IS '考勤记录表';
COMMENT ON COLUMN attendance_records.attend_date IS '考勤日期';
COMMENT ON COLUMN attendance_records.clock_in IS '上班打卡时间';
COMMENT ON COLUMN attendance_records.clock_out IS '下班打卡时间';
COMMENT ON COLUMN attendance_records.status IS '考勤状态';
COMMENT ON COLUMN attendance_records.late_minutes IS '迟到分钟数';
COMMENT ON COLUMN attendance_records.early_minutes IS '早退分钟数';
COMMENT ON COLUMN attendance_records.overtime_hours IS '加班小时数';
COMMENT ON COLUMN attendance_records.source IS '打卡来源';

CREATE INDEX idx_att_employee ON attendance_records(employee_id);
CREATE INDEX idx_att_date ON attendance_records(attend_date);
CREATE INDEX idx_att_status ON attendance_records(status);

-- ============================================================
-- 7. leave_requests — 请假申请表
-- ============================================================
CREATE TABLE leave_requests (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    employee_id     INT             NOT NULL,
    leave_type      VARCHAR2(20)    NOT NULL CHECK (leave_type IN ('年假','事假','病假','婚假','产假','陪产假','丧假','调休','工伤假')),
    start_date      DATE            NOT NULL,
    end_date        DATE            NOT NULL,
    total_days      NUMBER(4,1)    NOT NULL,
    reason          CLOB            DEFAULT NULL,
    status          VARCHAR2(20)    NOT NULL DEFAULT '待审批' CHECK (status IN ('待审批','审批通过','审批拒绝','已销假','已撤销')),
    approver_id     INT             DEFAULT NULL,
    approve_comment VARCHAR2(500)   DEFAULT NULL,
    approved_at     TIMESTAMP       DEFAULT NULL,
    created_at      TIMESTAMP       DEFAULT SYSDATE,
    updated_at      TIMESTAMP       DEFAULT SYSDATE,
    FOREIGN KEY (employee_id) REFERENCES employees(id),
    FOREIGN KEY (approver_id) REFERENCES employees(id),
    CHECK (end_date >= start_date)
);

COMMENT ON TABLE leave_requests IS '请假申请表';
COMMENT ON COLUMN leave_requests.leave_type IS '请假类型';
COMMENT ON COLUMN leave_requests.start_date IS '开始日期';
COMMENT ON COLUMN leave_requests.end_date IS '结束日期';
COMMENT ON COLUMN leave_requests.total_days IS '请假天数';
COMMENT ON COLUMN leave_requests.reason IS '请假原因';
COMMENT ON COLUMN leave_requests.status IS '审批状态';
COMMENT ON COLUMN leave_requests.approver_id IS '审批人ID';
COMMENT ON COLUMN leave_requests.approve_comment IS '审批意见';
COMMENT ON COLUMN leave_requests.approved_at IS '审批时间';

CREATE INDEX idx_leave_employee ON leave_requests(employee_id);
CREATE INDEX idx_leave_status ON leave_requests(status);
CREATE INDEX idx_leave_date ON leave_requests(start_date);
CREATE INDEX idx_leave_approver ON leave_requests(approver_id);

-- ============================================================
-- 8. trainings — 培训课程表
-- ============================================================
CREATE TABLE trainings (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    name            VARCHAR2(200)   NOT NULL,
    category        VARCHAR2(40)    NOT NULL CHECK (category IN ('技术培训','管理培训','安全培训','入职培训','合规培训','职业素养','其他')),
    trainer         VARCHAR2(80)    DEFAULT NULL,
    trainer_org     VARCHAR2(200)   DEFAULT NULL,
    location        VARCHAR2(200)   DEFAULT NULL,
    start_date      DATE            NOT NULL,
    end_date        DATE            NOT NULL,
    total_hours     NUMBER(5,1)    DEFAULT 0.0,
    max_capacity    INT             DEFAULT 30,
    enrolled_count  INT             DEFAULT 0,
    cost_per_person NUMBER(10,2)   DEFAULT 0.00,
    description     CLOB            DEFAULT NULL,
    status          VARCHAR2(20)    NOT NULL DEFAULT '计划中' CHECK (status IN ('计划中','报名中','进行中','已完成','已取消')),
    created_at      TIMESTAMP       DEFAULT SYSDATE,
    CHECK (end_date >= start_date)
);

COMMENT ON TABLE trainings IS '培训课程表';
COMMENT ON COLUMN trainings.name IS '培训名称';
COMMENT ON COLUMN trainings.category IS '培训类别';
COMMENT ON COLUMN trainings.trainer IS '培训讲师';
COMMENT ON COLUMN trainings.trainer_org IS '培训机构';
COMMENT ON COLUMN trainings.location IS '培训地点';
COMMENT ON COLUMN trainings.total_hours IS '总课时';
COMMENT ON COLUMN trainings.max_capacity IS '最大报名人数';
COMMENT ON COLUMN trainings.enrolled_count IS '已报名人数';
COMMENT ON COLUMN trainings.cost_per_person IS '人均费用';
COMMENT ON COLUMN trainings.status IS '培训状态';

CREATE INDEX idx_train_status ON trainings(status);
CREATE INDEX idx_train_date ON trainings(start_date);
CREATE INDEX idx_train_category ON trainings(category);

-- ============================================================
-- 9. employee_trainings — 员工培训关联表
-- ============================================================
CREATE TABLE employee_trainings (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    employee_id     INT             NOT NULL,
    training_id     INT             NOT NULL,
    enroll_date     DATE            NOT NULL DEFAULT SYSDATE,
    attend_status   VARCHAR2(20)    NOT NULL DEFAULT '已报名' CHECK (attend_status IN ('已报名','已签到','已完成','未参加','中途退出')),
    score           NUMBER(5,1)    DEFAULT NULL CHECK (score IS NULL OR (score >= 0 AND score <= 100)),
    evaluation      VARCHAR2(500)   DEFAULT NULL,
    certificate     VARCHAR2(200)   DEFAULT NULL,
    cost_amount     NUMBER(10,2)   DEFAULT 0.00,
    remark          VARCHAR2(300)   DEFAULT NULL,
    created_at      TIMESTAMP       DEFAULT SYSDATE,
    FOREIGN KEY (employee_id) REFERENCES employees(id),
    FOREIGN KEY (training_id) REFERENCES trainings(id),
    UNIQUE (employee_id, training_id)
);

COMMENT ON TABLE employee_trainings IS '员工培训关联表';
COMMENT ON COLUMN employee_trainings.employee_id IS '员工ID';
COMMENT ON COLUMN employee_trainings.training_id IS '培训ID';
COMMENT ON COLUMN employee_trainings.enroll_date IS '报名日期';
COMMENT ON COLUMN employee_trainings.attend_status IS '参与状态';
COMMENT ON COLUMN employee_trainings.score IS '考核成绩(0-100)';
COMMENT ON COLUMN employee_trainings.evaluation IS '培训评价';
COMMENT ON COLUMN employee_trainings.certificate IS '获得证书';

CREATE INDEX idx_et_employee ON employee_trainings(employee_id);
CREATE INDEX idx_et_training ON employee_trainings(training_id);
CREATE INDEX idx_et_status ON employee_trainings(attend_status);

-- ============================================================
-- 10. performance_reviews — 绩效考核表
-- ============================================================
CREATE TABLE performance_reviews (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    employee_id     INT             NOT NULL,
    review_year     INT             NOT NULL,
    review_quarter  INT             NOT NULL CHECK (review_quarter >= 1 AND review_quarter <= 4),
    reviewer_id     INT             NOT NULL,
    work_quality    NUMBER(3,1)    NOT NULL CHECK (work_quality >= 0 AND work_quality <= 10),
    work_efficiency NUMBER(3,1)    NOT NULL CHECK (work_efficiency >= 0 AND work_efficiency <= 10),
    teamwork        NUMBER(3,1)    NOT NULL CHECK (teamwork >= 0 AND teamwork <= 10),
    innovation      NUMBER(3,1)    NOT NULL CHECK (innovation >= 0 AND innovation <= 10),
    attendance_score NUMBER(3,1)   NOT NULL CHECK (attendance_score >= 0 AND attendance_score <= 10),
    total_score     NUMBER(5,1)    NOT NULL,
    grade           VARCHAR2(10)    NOT NULL CHECK (grade IN ('S','A','B','C','D')),
    self_evaluation CLOB            DEFAULT NULL,
    reviewer_comment CLOB           DEFAULT NULL,
    improvement_plan CLOB           DEFAULT NULL,
    status          VARCHAR2(20)    NOT NULL DEFAULT '草稿' CHECK (status IN ('草稿','待确认','已完成','已申诉')),
    confirmed_at    TIMESTAMP       DEFAULT NULL,
    created_at      TIMESTAMP       DEFAULT SYSDATE,
    updated_at      TIMESTAMP       DEFAULT SYSDATE,
    FOREIGN KEY (employee_id) REFERENCES employees(id),
    FOREIGN KEY (reviewer_id) REFERENCES employees(id),
    UNIQUE (employee_id, review_year, review_quarter)
);

COMMENT ON TABLE performance_reviews IS '绩效考核表';
COMMENT ON COLUMN performance_reviews.review_year IS '考核年份';
COMMENT ON COLUMN performance_reviews.review_quarter IS '考核季度(1-4)';
COMMENT ON COLUMN performance_reviews.reviewer_id IS '考核人ID';
COMMENT ON COLUMN performance_reviews.work_quality IS '工作质量评分(0-10)';
COMMENT ON COLUMN performance_reviews.work_efficiency IS '工作效率评分(0-10)';
COMMENT ON COLUMN performance_reviews.teamwork IS '团队协作评分(0-10)';
COMMENT ON COLUMN performance_reviews.innovation IS '创新能力评分(0-10)';
COMMENT ON COLUMN performance_reviews.attendance_score IS '考勤评分(0-10)';
COMMENT ON COLUMN performance_reviews.total_score IS '综合总分';
COMMENT ON COLUMN performance_reviews.grade IS '绩效等级: S/A/B/C/D';
COMMENT ON COLUMN performance_reviews.self_evaluation IS '自评';
COMMENT ON COLUMN performance_reviews.reviewer_comment IS '考核人评语';
COMMENT ON COLUMN performance_reviews.improvement_plan IS '改进计划';
COMMENT ON COLUMN performance_reviews.status IS '考核状态';

CREATE INDEX idx_pr_employee ON performance_reviews(employee_id);
CREATE INDEX idx_pr_reviewer ON performance_reviews(reviewer_id);
CREATE INDEX idx_pr_grade ON performance_reviews(grade);
CREATE INDEX idx_pr_period ON performance_reviews(review_year, review_quarter);

-- ============================================================
-- 更新 departments 中 manager_id 的外键约束
-- ============================================================
ALTER TABLE departments ADD CONSTRAINT fk_dept_manager
    FOREIGN KEY (manager_id) REFERENCES employees(id);

COMMIT;
