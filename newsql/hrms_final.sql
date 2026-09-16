-- ============================================================
-- HRMS Final Schema + Seed Data for Dameng
-- Strategy: tables without long Chinese CHECK constraints,
--           add short constraints via ALTER TABLE
-- ============================================================

-- ==================== SCHEMA ====================

-- 1. companies
CREATE TABLE companies (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    name            VARCHAR2(200)   NOT NULL,
    short_name      VARCHAR2(60),
    credit_code     VARCHAR2(30)    NOT NULL,
    legal_person    VARCHAR2(60),
    registered_capital NUMBER(16,2),
    establish_date  DATE,
    address         VARCHAR2(300),
    phone           VARCHAR2(30),
    industry        VARCHAR2(60),
    employee_count  INT             DEFAULT 0,
    status          VARCHAR2(20)    DEFAULT '正常',
    created_at      TIMESTAMP       DEFAULT SYSDATE,
    updated_at      TIMESTAMP       DEFAULT SYSDATE
);

-- 2. departments
CREATE TABLE departments (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    company_id      INT             NOT NULL,
    name            VARCHAR2(80)    NOT NULL,
    parent_id       INT,
    manager_id      INT,
    phone           VARCHAR2(30),
    floor           INT,
    budget          NUMBER(14,2)   DEFAULT 0.00,
    description     VARCHAR2(500),
    sort_order      INT             DEFAULT 0,
    status          VARCHAR2(20)    DEFAULT '启用',
    created_at      TIMESTAMP       DEFAULT SYSDATE,
    FOREIGN KEY (company_id) REFERENCES companies(id),
    FOREIGN KEY (parent_id) REFERENCES departments(id)
);
CREATE INDEX idx_dept_company ON departments(company_id);
CREATE INDEX idx_dept_parent ON departments(parent_id);

-- 3. positions
CREATE TABLE positions (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    name            VARCHAR2(60)    NOT NULL,
    department_id   INT,
    pos_rank        VARCHAR2(20)    NOT NULL DEFAULT '职员',
    min_salary      NUMBER(10,2),
    max_salary      NUMBER(10,2),
    responsibilities VARCHAR2(4000),
    requirements    VARCHAR2(4000),
    vacancy_count   INT             DEFAULT 0,
    status          VARCHAR2(20)    DEFAULT '启用',
    created_at      TIMESTAMP       DEFAULT SYSDATE,
    FOREIGN KEY (department_id) REFERENCES departments(id)
);
CREATE INDEX idx_pos_dept ON positions(department_id);
CREATE INDEX idx_pos_rank ON positions(pos_rank);

-- 4. employees
CREATE TABLE employees (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    employee_no     VARCHAR2(20)    NOT NULL,
    name            VARCHAR2(80)    NOT NULL,
    gender          CHAR(1)         NOT NULL,
    birth_date      DATE,
    id_card         VARCHAR2(18),
    phone           VARCHAR2(20),
    email           VARCHAR2(120),
    native_place    VARCHAR2(100),
    education       VARCHAR2(20),
    major           VARCHAR2(80),
    school          VARCHAR2(120),
    company_id      INT             NOT NULL,
    department_id   INT             NOT NULL,
    position_id     INT,
    hire_date       DATE            NOT NULL,
    contract_end    DATE,
    work_status     VARCHAR2(20)    NOT NULL DEFAULT '在职',
    marital_status  VARCHAR2(10)    DEFAULT '未婚',
    home_address    VARCHAR2(300),
    emergency_contact VARCHAR2(80),
    emergency_phone VARCHAR2(20),
    bank_name       VARCHAR2(100),
    bank_account    VARCHAR2(30),
    social_insurance VARCHAR2(30),
    created_at      TIMESTAMP       DEFAULT SYSDATE,
    updated_at      TIMESTAMP       DEFAULT SYSDATE,
    FOREIGN KEY (company_id) REFERENCES companies(id),
    FOREIGN KEY (department_id) REFERENCES departments(id),
    FOREIGN KEY (position_id) REFERENCES positions(id)
);
CREATE UNIQUE INDEX idx_emp_no ON employees(employee_no);
CREATE INDEX idx_emp_company ON employees(company_id);
CREATE INDEX idx_emp_dept ON employees(department_id);
CREATE INDEX idx_emp_pos ON employees(position_id);
CREATE INDEX idx_emp_status ON employees(work_status);
CREATE INDEX idx_emp_name ON employees(name);

-- 5. salary_records
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
    pay_status      VARCHAR2(20)   NOT NULL DEFAULT '待发放',
    paid_at         TIMESTAMP,
    remarks         VARCHAR2(500),
    created_at      TIMESTAMP       DEFAULT SYSDATE,
    FOREIGN KEY (employee_id) REFERENCES employees(id),
    UNIQUE (employee_id, salary_year, salary_month)
);
CREATE INDEX idx_sal_employee ON salary_records(employee_id);
CREATE INDEX idx_sal_period ON salary_records(salary_year, salary_month);

-- 6. attendance_records
CREATE TABLE attendance_records (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    employee_id     INT             NOT NULL,
    attend_date     DATE            NOT NULL,
    clock_in        TIMESTAMP,
    clock_out       TIMESTAMP,
    att_status      VARCHAR2(20)    NOT NULL DEFAULT '正常',
    late_minutes    INT             DEFAULT 0,
    early_minutes   INT             DEFAULT 0,
    overtime_hours  NUMBER(4,1)    DEFAULT 0.0,
    att_source      VARCHAR2(20)    DEFAULT '打卡机',
    remark          VARCHAR2(300),
    created_at      TIMESTAMP       DEFAULT SYSDATE,
    FOREIGN KEY (employee_id) REFERENCES employees(id),
    UNIQUE (employee_id, attend_date)
);
CREATE INDEX idx_att_employee ON attendance_records(employee_id);
CREATE INDEX idx_att_date ON attendance_records(attend_date);

-- 7. leave_requests
CREATE TABLE leave_requests (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    employee_id     INT             NOT NULL,
    leave_type      VARCHAR2(20)    NOT NULL,
    start_date      DATE            NOT NULL,
    end_date        DATE            NOT NULL,
    total_days      NUMBER(4,1)    NOT NULL,
    reason          VARCHAR2(4000),
    leave_status    VARCHAR2(20)    NOT NULL DEFAULT '待审批',
    approver_id     INT,
    approve_comment VARCHAR2(500),
    approved_at     TIMESTAMP,
    created_at      TIMESTAMP       DEFAULT SYSDATE,
    updated_at      TIMESTAMP       DEFAULT SYSDATE,
    FOREIGN KEY (employee_id) REFERENCES employees(id),
    FOREIGN KEY (approver_id) REFERENCES employees(id),
    CHECK (end_date >= start_date)
);
CREATE INDEX idx_leave_employee ON leave_requests(employee_id);
CREATE INDEX idx_leave_status ON leave_requests(leave_status);
CREATE INDEX idx_leave_date ON leave_requests(start_date);

-- 8. trainings
CREATE TABLE trainings (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    name            VARCHAR2(200)   NOT NULL,
    category        VARCHAR2(40)    NOT NULL,
    trainer         VARCHAR2(80),
    trainer_org     VARCHAR2(200),
    location        VARCHAR2(200),
    start_date      DATE            NOT NULL,
    end_date        DATE            NOT NULL,
    total_hours     NUMBER(5,1)    DEFAULT 0.0,
    max_capacity    INT             DEFAULT 30,
    enrolled_count  INT             DEFAULT 0,
    cost_per_person NUMBER(10,2)   DEFAULT 0.00,
    description     VARCHAR2(4000),
    train_status    VARCHAR2(20)    NOT NULL DEFAULT '计划中',
    created_at      TIMESTAMP       DEFAULT SYSDATE,
    CHECK (end_date >= start_date)
);
CREATE INDEX idx_train_status ON trainings(train_status);
CREATE INDEX idx_train_date ON trainings(start_date);

-- 9. employee_trainings
CREATE TABLE employee_trainings (
    id              INT IDENTITY(1,1) PRIMARY KEY,
    employee_id     INT             NOT NULL,
    training_id     INT             NOT NULL,
    enroll_date     DATE            NOT NULL DEFAULT SYSDATE,
    attend_status   VARCHAR2(20)    NOT NULL DEFAULT '已报名',
    score           NUMBER(5,1)    CHECK (score IS NULL OR (score >= 0 AND score <= 100)),
    evaluation      VARCHAR2(500),
    certificate     VARCHAR2(200),
    cost_amount     NUMBER(10,2)   DEFAULT 0.00,
    remark          VARCHAR2(300),
    created_at      TIMESTAMP       DEFAULT SYSDATE,
    FOREIGN KEY (employee_id) REFERENCES employees(id),
    FOREIGN KEY (training_id) REFERENCES trainings(id),
    UNIQUE (employee_id, training_id)
);
CREATE INDEX idx_et_employee ON employee_trainings(employee_id);
CREATE INDEX idx_et_training ON employee_trainings(training_id);

-- 10. performance_reviews
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
    self_evaluation VARCHAR2(4000),
    reviewer_comment VARCHAR2(4000),
    improvement_plan VARCHAR2(4000),
    review_status   VARCHAR2(20)    NOT NULL DEFAULT '草稿',
    confirmed_at    TIMESTAMP,
    created_at      TIMESTAMP       DEFAULT SYSDATE,
    updated_at      TIMESTAMP       DEFAULT SYSDATE,
    FOREIGN KEY (employee_id) REFERENCES employees(id),
    FOREIGN KEY (reviewer_id) REFERENCES employees(id),
    UNIQUE (employee_id, review_year, review_quarter)
);
CREATE INDEX idx_pr_employee ON performance_reviews(employee_id);
CREATE INDEX idx_pr_grade ON performance_reviews(grade);

ALTER TABLE departments ADD CONSTRAINT fk_dept_manager FOREIGN KEY (manager_id) REFERENCES employees(id);

-- ==================== SHORT CHECK CONSTRAINTS ====================
ALTER TABLE companies ADD CONSTRAINT ck_co_status CHECK (status = '正常' OR status = '注销' OR status = '吊销' OR status = '停业');
ALTER TABLE departments ADD CONSTRAINT ck_dept_status CHECK (status = '启用' OR status = '停用' OR status = '撤销');
ALTER TABLE positions ADD CONSTRAINT ck_pos_status CHECK (status = '启用' OR status = '停用' OR status = '已取消');

COMMIT;
