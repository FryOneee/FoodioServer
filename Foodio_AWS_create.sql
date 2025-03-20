-- Created by Vertabelo (http://vertabelo.com)
-- Last modification date: 2025-03-18 21:29:10.453

-- tables
-- Table: Goal
CREATE TABLE Goal (
    ID int  NOT NULL,
    User_ID int  NOT NULL,
    kcal int  NULL,
    protein int  NULL,
    fats int  NULL,
    carbs int  NULL,
    desiredWeight decimal(3,1)  NOT NULL,
    lifestyle varchar(50)  NOT NULL,
    diet varchar(70)  NOT NULL,
    startDate date  NOT NULL,
    endDate date  NOT NULL,
    CONSTRAINT Goal_pk PRIMARY KEY (ID)
);

-- Table: Meal
CREATE TABLE Meal (
    ID int  NOT NULL,
    User_ID int  NOT NULL,
    bar_code varchar(100)  NOT NULL,
    img_link varchar(255)  NOT NULL,
    kcal int  NOT NULL,
    proteins int  NOT NULL,
    carbs int  NOT NULL,
    fats int  NOT NULL,
    date timestamp  NOT NULL,
    healthy_index int  NOT NULL,
    latitude decimal(9,6)  NULL,
    longitude decimal(9,6)  NULL,
    added boolean  NULL,
    CONSTRAINT Meal_pk PRIMARY KEY (ID)
);

-- Table: OpenAI_request
CREATE TABLE OpenAI_request (
    ID int  NOT NULL,
    User_ID int  NOT NULL,
    type char(1)  NOT NULL,
    img_link varchar(255)  NULL,
    date timestamp  NOT NULL,
    CONSTRAINT OpenAI_request_pk PRIMARY KEY (ID)
);

-- Table: Problem
CREATE TABLE Problem (
    ID int  NOT NULL,
    User_ID int  NOT NULL,
    description varchar(100)  NOT NULL,
    CONSTRAINT Problem_pk PRIMARY KEY (ID)
);

-- Table: Subscription
CREATE TABLE Subscription (
    ID int  NOT NULL,
    User_ID int  NOT NULL,
    subscription_type int  NOT NULL,
    original_transaction_id text  NOT NULL,
    isActive char(1)  NOT NULL,
    CONSTRAINT Subscription_pk PRIMARY KEY (ID)
);

-- Table: User
CREATE TABLE "User" (
    ID int  NOT NULL,
    email varchar(255)  NOT NULL,
    password varchar(255)  NULL,
    sex char(1)  NULL,
    birthDate date  NULL,
    height int  NULL,
    dateOfJoin date  NOT NULL,
    CONSTRAINT User_pk PRIMARY KEY (ID)
);

-- Table: Warning
CREATE TABLE Warning (
    ID int  NOT NULL,
    Meal_ID int  NOT NULL,
    warning text  NOT NULL,
    CONSTRAINT Warning_pk PRIMARY KEY (ID)
);

-- Table: Weight
CREATE TABLE Weight (
    ID int  NOT NULL,
    User_ID int  NOT NULL,
    weight decimal(3,1)  NOT NULL,
    date date  NOT NULL,
    CONSTRAINT Weight_pk PRIMARY KEY (ID)
);

-- foreign keys
-- Reference: Goal_User (table: Goal)
ALTER TABLE Goal ADD CONSTRAINT Goal_User
    FOREIGN KEY (User_ID)
    REFERENCES "User" (ID)
    NOT DEFERRABLE
    INITIALLY IMMEDIATE
;

-- Reference: OpenAI_request_User (table: OpenAI_request)
ALTER TABLE OpenAI_request ADD CONSTRAINT OpenAI_request_User
    FOREIGN KEY (User_ID)
    REFERENCES "User" (ID)
    NOT DEFERRABLE
    INITIALLY IMMEDIATE
;

-- Reference: Problem_User (table: Problem)
ALTER TABLE Problem ADD CONSTRAINT Problem_User
    FOREIGN KEY (User_ID)
    REFERENCES "User" (ID)
    NOT DEFERRABLE
    INITIALLY IMMEDIATE
;

-- Reference: Subscription_User (table: Subscription)
ALTER TABLE Subscription ADD CONSTRAINT Subscription_User
    FOREIGN KEY (User_ID)
    REFERENCES "User" (ID)
    NOT DEFERRABLE
    INITIALLY IMMEDIATE
;

-- Reference: Table_2_User (table: Meal)
ALTER TABLE Meal ADD CONSTRAINT Table_2_User
    FOREIGN KEY (User_ID)
    REFERENCES "User" (ID)
    NOT DEFERRABLE
    INITIALLY IMMEDIATE
;

-- Reference: Warning_Meal (table: Warning)
ALTER TABLE Warning ADD CONSTRAINT Warning_Meal
    FOREIGN KEY (Meal_ID)
    REFERENCES Meal (ID)
    NOT DEFERRABLE 
    INITIALLY IMMEDIATE
;

-- Reference: Weight_User (table: Weight)
ALTER TABLE Weight ADD CONSTRAINT Weight_User
    FOREIGN KEY (User_ID)
    REFERENCES "User" (ID)  
    NOT DEFERRABLE 
    INITIALLY IMMEDIATE
;

-- End of file.

