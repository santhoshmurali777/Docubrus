"""
config_model.py -- Customisation and schema model for Docroc.

Supports:
  - Document definitions (accepted formats, expiry, max age)
  - Extracted fields
  - Cross-check groups ('name', 'date', 'number', 'address', 'exact', 'masked_tail')
  - Rules (min/max age, minimum marks, cutoff date, required documents)
  - Delivery modes: 'deliver_files' vs 'data_only' (wipe PDFs after analysis)
  - Institutional branding & retention settings
  - Presets: College, Government Office, Blank
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class DocumentDef(BaseModel):
    id: str
    title: str
    description: str = ""
    required: bool = True
    accepted_formats: List[str] = Field(default_factory=lambda: ["pdf", "jpg", "jpeg", "png"])
    max_age_days: Optional[int] = None       # e.g., income certificate valid for 365 days
    check_expiry: bool = False
    expected_keywords: List[str] = Field(default_factory=list)


class FieldDef(BaseModel):
    key: str                                 # e.g. "student_name", "dob"
    label: str                               # e.g. "Student Full Name"
    doc_type: str                            # e.g. "marksheet_10"
    data_type: Literal["text", "date", "number", "score", "address", "id"] = "text"
    required: bool = True


class CrossCheckGroup(BaseModel):
    name: str                                # e.g. "Candidate Name"
    match_type: Literal["name", "date", "number", "address", "exact", "masked_tail"] = "exact"
    fields: List[str]                        # ["marksheet_10.student_name", "marksheet_12.student_name", "aadhaar.name"]
    description: str = ""


class RuleDef(BaseModel):
    id: str
    title: str
    rule_type: Literal["min_age", "max_age", "min_score", "max_score", "required_all"]
    field_or_doc: str
    value: Any
    message: str


class CustomiseConfig(BaseModel):
    preset_name: str = "College"
    institution_name: str = "Docroc Verification System"
    brand_color: str = "#2250d8"
    logo_text: str = "Docroc"
    delivery_mode: Literal["deliver_files", "data_only"] = "deliver_files"
    retention_days: int = 30
    strictness: Literal["strict", "lenient"] = "strict"
    documents: List[DocumentDef] = Field(default_factory=list)
    fields: List[FieldDef] = Field(default_factory=list)
    cross_checks: List[CrossCheckGroup] = Field(default_factory=list)
    rules: List[RuleDef] = Field(default_factory=list)

    def sanitized_checklist(self) -> List[Dict[str, Any]]:
        """Safe view for applicants: reveals what documents are needed, without exposing cross-checks or rules."""
        return [
            {
                "id": d.id,
                "title": d.title,
                "description": d.description,
                "required": d.required,
                "accepted_formats": d.accepted_formats,
                "check_expiry": d.check_expiry,
                "max_age_days": d.max_age_days,
            }
            for d in self.documents
        ]


# ---------------- Presets ---------------------------------------------------------------------------------

PRESET_COLLEGE = CustomiseConfig(
    preset_name="College",
    institution_name="College Admissions Office",
    brand_color="#1e40af",
    logo_text="Admissions Verification",
    delivery_mode="deliver_files",
    retention_days=60,
    strictness="strict",
    documents=[
        DocumentDef(
            id="marksheet_10",
            title="Class 10 Marksheet / Certificate",
            description="Proof of Date of Birth and secondary schooling",
            required=True,
            expected_keywords=["secondary", "class x", "10th", "board", "marks", "date of birth", "dob"],
        ),
        DocumentDef(
            id="marksheet_12",
            title="Class 12 Marksheet",
            description="Higher secondary academic marksheet",
            required=True,
            expected_keywords=["higher secondary", "class xii", "12th", "board", "marks", "total", "percentage"],
        ),
        DocumentDef(
            id="identity_card",
            title="Identity Proof (Aadhaar / Passport / Voter ID)",
            description="Government issued photo identification",
            required=True,
            expected_keywords=["government", "unique identification", "identity", "aadhaar", "passport", "republic"],
        ),
        DocumentDef(
            id="transfer_cert",
            title="Transfer / Leaving Certificate",
            description="School/College leaving or transfer certificate",
            required=False,
            expected_keywords=["transfer", "leaving", "school", "institution", "conduct"],
        ),
        DocumentDef(
            id="category_cert",
            title="Category / Domicile Certificate",
            description="Applicable for reserved quota or state quota",
            required=False,
            check_expiry=True,
            max_age_days=365,
            expected_keywords=["certificate", "category", "caste", "domicile", "authority"],
        ),
    ],
    fields=[
        FieldDef(key="student_name", label="Student Full Name", doc_type="marksheet_10", data_type="text"),
        FieldDef(key="dob", label="Date of Birth", doc_type="marksheet_10", data_type="date"),
        FieldDef(key="student_name", label="Student Full Name", doc_type="marksheet_12", data_type="text"),
        FieldDef(key="marks_aggregate", label="Aggregate Percentage / Marks", doc_type="marksheet_12", data_type="score"),
        FieldDef(key="holder_name", label="Name on ID", doc_type="identity_card", data_type="text"),
        FieldDef(key="dob", label="Date of Birth", doc_type="identity_card", data_type="date"),
        FieldDef(key="id_number", label="ID Number", doc_type="identity_card", data_type="id"),
    ],
    cross_checks=[
        CrossCheckGroup(
            name="Candidate Name Consistency",
            match_type="name",
            fields=["marksheet_10.student_name", "marksheet_12.student_name", "identity_card.holder_name"],
            description="Candidate name must match across 10th marksheet, 12th marksheet, and ID proof regardless of word order or initials.",
        ),
        CrossCheckGroup(
            name="Date of Birth Consistency",
            match_type="date",
            fields=["marksheet_10.dob", "identity_card.dob"],
            description="Date of birth must be identical across Class 10 certificate and Government ID.",
        ),
    ],
    rules=[
        RuleDef(
            id="rule_min_marks_12",
            title="Class 12 Minimum Aggregate",
            rule_type="min_score",
            field_or_doc="marksheet_12.marks_aggregate",
            value=50.0,
            message="Class 12 marks must be at least 50% for standard admission eligibility.",
        ),
        RuleDef(
            id="rule_min_age",
            title="Minimum Age Requirement",
            rule_type="min_age",
            field_or_doc="marksheet_10.dob",
            value=16,
            message="Applicant must be at least 16 years of age on admission date.",
        ),
    ],
)

PRESET_GOVERNMENT = CustomiseConfig(
    preset_name="Government Office",
    institution_name="Public Services Licensing Authority",
    brand_color="#047857",
    logo_text="GovDesk",
    delivery_mode="data_only",
    retention_days=7,
    strictness="strict",
    documents=[
        DocumentDef(
            id="id_proof",
            title="Official Photo Identity Document",
            description="Valid passport, voter card, or national ID",
            required=True,
            check_expiry=True,
            expected_keywords=["republic", "government", "identity", "card", "department"],
        ),
        DocumentDef(
            id="address_proof",
            title="Proof of Residence / Address",
            description="Electricity bill, rental deed, or domicile pass (under 90 days)",
            required=True,
            max_age_days=90,
            expected_keywords=["bill", "address", "resident", "consumer", "deed", "authority"],
        ),
        DocumentDef(
            id="income_certificate",
            title="Annual Income Certificate",
            description="Issued by Revenue Officer or competent revenue authority",
            required=True,
            check_expiry=True,
            max_age_days=365,
            expected_keywords=["income", "revenue", "annual", "certificate", "tahsildar"],
        ),
    ],
    fields=[
        FieldDef(key="applicant_name", label="Full Name", doc_type="id_proof", data_type="text"),
        FieldDef(key="dob", label="Date of Birth", doc_type="id_proof", data_type="date"),
        FieldDef(key="address", label="Residential Address", doc_type="address_proof", data_type="address"),
        FieldDef(key="resident_name", label="Name on Bill/Deed", doc_type="address_proof", data_type="text"),
        FieldDef(key="beneficiary_name", label="Beneficiary Name", doc_type="income_certificate", data_type="text"),
        FieldDef(key="annual_income", label="Annual Income Amount", doc_type="income_certificate", data_type="number"),
    ],
    cross_checks=[
        CrossCheckGroup(
            name="Identity and Revenue Name Match",
            match_type="name",
            fields=["id_proof.applicant_name", "address_proof.resident_name", "income_certificate.beneficiary_name"],
            description="Name on ID, address utility, and income certificate must match.",
        ),
    ],
    rules=[
        RuleDef(
            id="rule_income_limit",
            title="Income Ceiling Verification",
            rule_type="max_score",
            field_or_doc="income_certificate.annual_income",
            value=800000.0,
            message="Annual income must not exceed 8 Lakhs for eligible status.",
        ),
    ],
)

PRESET_BLANK = CustomiseConfig(
    preset_name="Blank",
    institution_name="Custom Document Verification",
    brand_color="#334155",
    logo_text="Docroc Custom",
    delivery_mode="deliver_files",
    retention_days=14,
    strictness="lenient",
    documents=[
        DocumentDef(id="doc_1", title="Primary Document", description="First document to verify", required=True),
        DocumentDef(id="doc_2", title="Secondary Document", description="Supporting document", required=False),
    ],
    fields=[
        FieldDef(key="name", label="Name", doc_type="doc_1", data_type="text"),
        FieldDef(key="name", label="Name", doc_type="doc_2", data_type="text"),
    ],
    cross_checks=[
        CrossCheckGroup(
            name="Name Cross-Check",
            match_type="name",
            fields=["doc_1.name", "doc_2.name"],
            description="Check that both documents share the same name.",
        ),
    ],
    rules=[],
)

# ---------------- Full AICTE Approval Process Catalog & Preset --------------------------------------------

AICTE_CERTIFICATES_CATALOG: List[DocumentDef] = [
    DocumentDef(
        id="application_form",
        title="AICTE Part-I Application & Declaration",
        description="Official council application with Trust registration, program category, and proposed intake.",
        required=True,
        expected_keywords=["aicte", "all india council", "application", "intake", "trust", "society", "institution", "declaration"],
    ),
    DocumentDef(
        id="society_registration",
        title="Society / Trust / Sec 8 Company Registration",
        description="Certificate of registration, Memorandum of Association (MOA) and Trust Deed.",
        required=True,
        expected_keywords=["society", "trust", "registration", "registrar", "societies", "act", "memorandum", "byelaws"],
    ),
    DocumentDef(
        id="land_deed",
        title="Land Title Deed / Registered 30-Year Lease",
        description="Registered sale deed or government lease deed in the name of the Applicant Society/Trust.",
        required=True,
        expected_keywords=["deed", "sale deed", "lease", "sub-registrar", "survey", "acres", "purchaser", "schedule"],
    ),
    DocumentDef(
        id="land_use_certificate",
        title="Land Use Conversion Certificate",
        description="Revenue authority order permitting educational / institutional land use.",
        required=True,
        expected_keywords=["land use", "conversion", "revenue", "collector", "tahsildar", "educational", "order", "proceedings"],
    ),
    DocumentDef(
        id="encumbrance_certificate",
        title="Encumbrance Certificate (Non-Encumbrance)",
        description="Certificate from Sub-Registrar confirming clear title without bank mortgage or dispute.",
        required=True,
        max_age_days=90,
        expected_keywords=["encumbrance", "certificate", "sub-registrar", "nil encumbrance", "schedule", "clear title"],
    ),
    DocumentDef(
        id="building_plan",
        title="Approved Building & Campus Master Plan",
        description="Floor drawings and total built-up area approved by municipal corporation, DTCP or Panchayat.",
        required=True,
        expected_keywords=["building plan", "approved", "architect", "built-up", "square metres", "local body", "sanction"],
    ),
    DocumentDef(
        id="occupancy_certificate",
        title="Occupancy / Building Completion Certificate",
        description="Certificate from local municipal engineer verifying physical completion and fitness for occupation.",
        required=True,
        expected_keywords=["occupancy", "completion", "fit for occupation", "local body", "engineer", "municipal", "competent"],
    ),
    DocumentDef(
        id="fire_noc",
        title="Fire Safety Certificate (Fire NOC)",
        description="NOC from State Fire and Rescue Services Department with valid validity period.",
        required=True,
        check_expiry=True,
        expected_keywords=["fire", "safety", "noc", "fire and rescue", "advisory", "valid", "clearance"],
    ),
    DocumentDef(
        id="structural_stability",
        title="Structural Stability Certificate",
        description="Certificate from a certified Chartered Structural Engineer certifying earthquake and wind resistance.",
        required=True,
        expected_keywords=["structural", "stability", "chartered engineer", "earthquake", "load", "safe", "soundness"],
    ),
    DocumentDef(
        id="state_noc",
        title="State Government NOC / Essentiality Certificate",
        description="Essentiality certificate & NOC from State Department of Higher and Technical Education.",
        required=True,
        expected_keywords=["government", "higher education", "technical education", "noc", "essentiality", "sanction", "approval"],
    ),
    DocumentDef(
        id="university_affiliation_noc",
        title="Affiliating University NOC",
        description="No Objection Certificate from the affiliating State Technical University for degree granting.",
        required=True,
        expected_keywords=["university", "affiliation", "noc", "registrar", "academic council", "syndicate", "provisional"],
    ),
    DocumentDef(
        id="financial_statement",
        title="Audited Balance Sheet & CA Certificate (UDIN)",
        description="Audited financial statement certified by a Chartered Accountant with valid UDIN.",
        required=True,
        expected_keywords=["chartered accountant", "balance sheet", "audit", "membership", "udin", "assets", "liabilities"],
    ),
    DocumentDef(
        id="barrier_free_cert",
        title="Barrier-Free Built Environment & Ramp Certificate",
        description="Architect certificate confirming ramps, lifts, and disabled-friendly facilities (PWD Act compliance).",
        required=False,
        expected_keywords=["barrier free", "ramp", "pwd", "differently abled", "architect", "disabled", "toilets"],
    ),
    DocumentDef(
        id="aicte_affidavit",
        title="AICTE Omnibus Notarized Affidavit",
        description="Notarized undertaking on stamp paper regarding AICTE norms, anti-ragging, and faculty cadre ratios.",
        required=True,
        expected_keywords=["affidavit", "notary", "solemnly", "undertaking", "anti ragging", "norms", "deponent"],
    ),
]

PRESET_AICTE = CustomiseConfig(
    preset_name="AICTE Approval",
    institution_name="AICTE Technical Institute Approval Office",
    brand_color="#d97706",
    logo_text="AICTE Verification",
    delivery_mode="deliver_files",
    retention_days=90,
    strictness="strict",
    documents=AICTE_CERTIFICATES_CATALOG,
    fields=[
        FieldDef(key="applicant_name", label="Applicant Trust/Society Name", doc_type="application_form", data_type="text"),
        FieldDef(key="registration_number", label="Registration Number", doc_type="application_form", data_type="id"),
        FieldDef(key="survey_no", label="Survey Number", doc_type="application_form", data_type="id"),
        FieldDef(key="land_area_acres", label="Total Land Area (Acres)", doc_type="application_form", data_type="number"),
        FieldDef(key="built_up_area_sqm", label="Built-up Area (Sq M)", doc_type="application_form", data_type="number"),
        FieldDef(key="approved_intake", label="Sanctioned Intake", doc_type="application_form", data_type="number"),
        FieldDef(key="society_name", label="Registered Society Name", doc_type="society_registration", data_type="text"),
        FieldDef(key="registration_number", label="Society Reg Number", doc_type="society_registration", data_type="id"),
        FieldDef(key="owner_name", label="Purchaser / Land Owner", doc_type="land_deed", data_type="text"),
        FieldDef(key="survey_no", label="Land Survey Number", doc_type="land_deed", data_type="id"),
        FieldDef(key="land_area_acres", label="Deed Area (Acres)", doc_type="land_deed", data_type="number"),
        FieldDef(key="survey_no", label="Survey Number", doc_type="building_plan", data_type="id"),
        FieldDef(key="built_up_area_sqm", label="Approved Built-up (Sq M)", doc_type="building_plan", data_type="number"),
        FieldDef(key="built_up_area_sqm", label="Completed Area (Sq M)", doc_type="occupancy_certificate", data_type="number"),
        FieldDef(key="institution_name", label="Institution Name", doc_type="fire_noc", data_type="text"),
        FieldDef(key="expiry_date", label="Fire NOC Expiry Date", doc_type="fire_noc", data_type="date"),
        FieldDef(key="approved_intake", label="Recommended Intake", doc_type="state_noc", data_type="number"),
    ],
    cross_checks=[
        CrossCheckGroup(
            name="Trust / Applicant Legal Name",
            match_type="name",
            fields=["application_form.applicant_name", "society_registration.society_name", "land_deed.owner_name"],
            description="Applicant name must match across Council application, Society registration, and Land deed.",
        ),
        CrossCheckGroup(
            name="Land Survey Number Consistency",
            match_type="exact",
            fields=["application_form.survey_no", "land_deed.survey_no", "building_plan.survey_no"],
            description="Land survey numbers must agree across application form, title deed, and approved master building plan.",
        ),
        CrossCheckGroup(
            name="Built-up Area Consistency",
            match_type="exact",
            fields=["application_form.built_up_area_sqm", "building_plan.built_up_area_sqm", "occupancy_certificate.built_up_area_sqm"],
            description="Total built-up area must match between building approval plan and local body occupancy certificate.",
        ),
        CrossCheckGroup(
            name="Sanctioned Intake Reconciliation",
            match_type="exact",
            fields=["application_form.approved_intake", "state_noc.approved_intake"],
            description="Proposed intake in AICTE application must align with State Government NOC recommendation.",
        ),
    ],
    rules=[
        RuleDef(
            id="rule_min_land_area",
            title="Minimum Land Requirement (AICTE Norm)",
            rule_type="min_score",
            field_or_doc="application_form.land_area_acres",
            value=2.5,
            message="Technical institutes must possess at least 2.5 contiguous acres of land.",
        ),
        RuleDef(
            id="rule_min_built_up",
            title="Minimum Built-up Area Threshold",
            rule_type="min_score",
            field_or_doc="application_form.built_up_area_sqm",
            value=4000.0,
            message="Built-up instructional and administrative area must meet minimum AICTE floor threshold (4,000 sq m).",
        ),
    ],
)

PRESETS = {
    "College": PRESET_COLLEGE,
    "Government Office": PRESET_GOVERNMENT,
    "AICTE Approval": PRESET_AICTE,
    "Blank": PRESET_BLANK,
}
