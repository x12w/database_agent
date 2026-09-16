-- ============================================================
-- Hospital Management System — PostgreSQL Schema
-- Tables: departments, doctors, patients, drugs,
--         appointments, medical_records, prescriptions,
--         prescription_items, wards, inpatient_records
-- ============================================================

DROP DATABASE IF EXISTS hospital_db;
CREATE DATABASE hospital_db ENCODING 'UTF8';
\c hospital_db

-- ============================================================
-- 1. departments — 科室表
-- ============================================================
CREATE TABLE departments (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(80)     NOT NULL UNIQUE,
    floor       INT             DEFAULT NULL,
    phone       VARCHAR(20)     DEFAULT NULL,
    description TEXT            DEFAULT NULL,
    created_at  TIMESTAMP       DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- 2. doctors — 医生表
-- ============================================================
CREATE TABLE doctors (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(80)     NOT NULL,
    gender          CHAR(1)         CHECK (gender IN ('M','F')),
    title           VARCHAR(40)     NOT NULL DEFAULT '主治医师',
    department_id   INT             NOT NULL REFERENCES departments(id),
    phone           VARCHAR(20)     DEFAULT NULL,
    email           VARCHAR(120)    DEFAULT NULL UNIQUE,
    specialty       VARCHAR(200)    DEFAULT NULL,
    consultation_fee DECIMAL(8,2)  NOT NULL DEFAULT 30.00,
    hire_date       DATE            DEFAULT NULL,
    active          BOOLEAN         DEFAULT TRUE,
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_doctors_dept ON doctors(department_id);

-- ============================================================
-- 3. patients — 患者表
-- ============================================================
CREATE TABLE patients (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(80)     NOT NULL,
    gender          CHAR(1)         CHECK (gender IN ('M','F')),
    birth_date      DATE            DEFAULT NULL,
    id_card         VARCHAR(18)     DEFAULT NULL UNIQUE,
    phone           VARCHAR(20)     DEFAULT NULL,
    address         TEXT            DEFAULT NULL,
    blood_type      VARCHAR(4)      DEFAULT NULL,
    has_allergy     BOOLEAN         DEFAULT FALSE,
    allergy_desc    TEXT            DEFAULT NULL,
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_patients_phone ON patients(phone);

-- ============================================================
-- 4. drugs — 药品目录表
-- ============================================================
CREATE TABLE drugs (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(120)    NOT NULL,
    generic_name    VARCHAR(120)    DEFAULT NULL,
    category        VARCHAR(40)     NOT NULL DEFAULT '化学药品',
    form            VARCHAR(30)     NOT NULL DEFAULT '片剂',
    specification   VARCHAR(80)     NOT NULL,
    unit            VARCHAR(10)     NOT NULL DEFAULT '盒',
    unit_price      DECIMAL(10,2)  NOT NULL CHECK (unit_price > 0),
    manufacturer    VARCHAR(120)    DEFAULT NULL,
    stock_quantity  INT             NOT NULL DEFAULT 0,
    prescription_only BOOLEAN       DEFAULT TRUE,
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_drugs_category ON drugs(category);

-- ============================================================
-- 5. appointments — 预约挂号表
-- ============================================================
CREATE TABLE appointments (
    id              SERIAL PRIMARY KEY,
    patient_id      INT             NOT NULL REFERENCES patients(id),
    doctor_id       INT             NOT NULL REFERENCES doctors(id),
    department_id   INT             NOT NULL REFERENCES departments(id),
    appoint_date    DATE            NOT NULL,
    time_slot       VARCHAR(10)     NOT NULL CHECK (time_slot IN ('上午','下午','夜间')),
    status          VARCHAR(20)     NOT NULL DEFAULT '已预约'
                    CHECK (status IN ('已预约','已签到','已就诊','已取消','爽约')),
    chief_complaint TEXT            DEFAULT NULL,
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_appt_date ON appointments(appoint_date);
CREATE INDEX idx_appt_patient ON appointments(patient_id);
CREATE INDEX idx_appt_doctor ON appointments(doctor_id);
CREATE INDEX idx_appt_status ON appointments(status);

-- ============================================================
-- 6. medical_records — 病历记录表
-- ============================================================
CREATE TABLE medical_records (
    id              SERIAL PRIMARY KEY,
    patient_id      INT             NOT NULL REFERENCES patients(id),
    doctor_id       INT             NOT NULL REFERENCES doctors(id),
    appointment_id  INT             DEFAULT NULL REFERENCES appointments(id),
    visit_date      DATE            NOT NULL DEFAULT CURRENT_DATE,
    diagnosis       TEXT            NOT NULL,
    symptoms        TEXT            DEFAULT NULL,
    treatment_plan  TEXT            DEFAULT NULL,
    notes           TEXT            DEFAULT NULL,
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_mr_patient ON medical_records(patient_id);
CREATE INDEX idx_mr_doctor ON medical_records(doctor_id);
CREATE INDEX idx_mr_date ON medical_records(visit_date);

-- ============================================================
-- 7. prescriptions — 处方表
-- ============================================================
CREATE TABLE prescriptions (
    id              SERIAL PRIMARY KEY,
    record_id       INT             NOT NULL REFERENCES medical_records(id),
    patient_id      INT             NOT NULL REFERENCES patients(id),
    doctor_id       INT             NOT NULL REFERENCES doctors(id),
    total_amount    DECIMAL(10,2)  NOT NULL DEFAULT 0.00,
    status          VARCHAR(20)     NOT NULL DEFAULT '未取药'
                    CHECK (status IN ('未取药','已取药','已退药')),
    prescribed_at   TIMESTAMP       DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_rx_record ON prescriptions(record_id);

-- ============================================================
-- 8. prescription_items — 处方明细表
-- ============================================================
CREATE TABLE prescription_items (
    id              SERIAL PRIMARY KEY,
    prescription_id INT             NOT NULL REFERENCES prescriptions(id) ON DELETE CASCADE,
    drug_id         INT             NOT NULL REFERENCES drugs(id),
    dosage          VARCHAR(100)    NOT NULL,
    frequency       VARCHAR(80)     NOT NULL,
    days            INT             NOT NULL DEFAULT 3 CHECK (days > 0),
    quantity        INT             NOT NULL CHECK (quantity > 0),
    unit_price      DECIMAL(10,2)  NOT NULL,
    subtotal        DECIMAL(10,2)  GENERATED ALWAYS AS (quantity * unit_price) STORED
);
CREATE INDEX idx_rx_items_rx ON prescription_items(prescription_id);

-- ============================================================
-- 9. wards — 病房表
-- ============================================================
CREATE TABLE wards (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(40)     NOT NULL UNIQUE,
    department_id   INT             NOT NULL REFERENCES departments(id),
    room_type       VARCHAR(20)     NOT NULL CHECK (room_type IN ('普通病房','双人间','单人间','VIP病房','ICU')),
    total_beds      INT             NOT NULL DEFAULT 4 CHECK (total_beds > 0),
    occupied_beds   INT             NOT NULL DEFAULT 0 CHECK (occupied_beds >= 0),
    daily_rate      DECIMAL(8,2)   NOT NULL DEFAULT 0.00,
    CHECK (occupied_beds <= total_beds)
);

-- ============================================================
-- 10. inpatient_records — 住院记录表
-- ============================================================
CREATE TABLE inpatient_records (
    id              SERIAL PRIMARY KEY,
    patient_id      INT             NOT NULL REFERENCES patients(id),
    doctor_id       INT             NOT NULL REFERENCES doctors(id),
    ward_id         INT             NOT NULL REFERENCES wards(id),
    bed_number      INT             NOT NULL,
    admit_date      DATE            NOT NULL,
    discharge_date  DATE            DEFAULT NULL,
    admission_diag  TEXT            DEFAULT NULL,
    discharge_diag  TEXT            DEFAULT NULL,
    status          VARCHAR(20)     NOT NULL DEFAULT '住院中'
                    CHECK (status IN ('住院中','已出院','转科','死亡')),
    total_cost      DECIMAL(12,2)  DEFAULT 0.00,
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    CHECK (discharge_date IS NULL OR discharge_date >= admit_date)
);
CREATE INDEX idx_ip_patient ON inpatient_records(patient_id);
CREATE INDEX idx_ip_status ON inpatient_records(status);
