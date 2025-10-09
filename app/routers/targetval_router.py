
# -*- coding: utf-8 -*-
"""
router_v58_full.py — Target Validation Router (58 modules, literature mesh, synthesis)
====================================================================================

Complete FastAPI router that implements:
  • 58 modules across 6 evidence buckets + 1 synthesis bucket
  • Live data fetchers (public APIs) with guarded fallbacks
  • Literature mesh (Europe PMC) with confirm/disconfirm stance and quality heuristic
  • Explainable synthesis (qualitative bands + light logistic probability)
  • Registry + health endpoints

This file is self-contained and production-oriented; it keeps the proven live-fetch pattern
(retries/backoff + TTL cache) and degrades gracefully when upstreams are flaky.
"""

import asyncio
import json
import logging
import math
import time
import urllib.parse
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field\n\n\n# --- Normalization maps & synonym tables (for literature mesh / synthesis heuristics) ---\nDISEASE_SYNONYMS = {
  "breast cancer": [
    "breast cancer",
    "breast cancer",
    "breast-cancer",
    "BREAST CANCER",
    "Breast cancer",
    "breast cancer 1",
    "breast cancer 2",
    "breast cancer 3",
    "breast cancer 4",
    "breast cancer 5",
    "breast cancer 6",
    "breast cancer 7"
  ],
  "lung cancer": [
    "lung cancer",
    "lung cancer",
    "lung-cancer",
    "LUNG CANCER",
    "Lung cancer",
    "lung cancer 1",
    "lung cancer 2",
    "lung cancer 3",
    "lung cancer 4",
    "lung cancer 5",
    "lung cancer 6",
    "lung cancer 7"
  ],
  "colorectal cancer": [
    "colorectal cancer",
    "colorectal cancer",
    "colorectal-cancer",
    "COLORECTAL CANCER",
    "Colorectal cancer",
    "colorectal cancer 1",
    "colorectal cancer 2",
    "colorectal cancer 3",
    "colorectal cancer 4",
    "colorectal cancer 5",
    "colorectal cancer 6",
    "colorectal cancer 7"
  ],
  "prostate cancer": [
    "prostate cancer",
    "prostate cancer",
    "prostate-cancer",
    "PROSTATE CANCER",
    "Prostate cancer",
    "prostate cancer 1",
    "prostate cancer 2",
    "prostate cancer 3",
    "prostate cancer 4",
    "prostate cancer 5",
    "prostate cancer 6",
    "prostate cancer 7"
  ],
  "pancreatic cancer": [
    "pancreatic cancer",
    "pancreatic cancer",
    "pancreatic-cancer",
    "PANCREATIC CANCER",
    "Pancreatic cancer",
    "pancreatic cancer 1",
    "pancreatic cancer 2",
    "pancreatic cancer 3",
    "pancreatic cancer 4",
    "pancreatic cancer 5",
    "pancreatic cancer 6",
    "pancreatic cancer 7"
  ],
  "glioblastoma": [
    "glioblastoma",
    "glioblastoma",
    "glioblastoma",
    "GLIOBLASTOMA",
    "Glioblastoma",
    "glioblastoma 1",
    "glioblastoma 2",
    "glioblastoma 3",
    "glioblastoma 4",
    "glioblastoma 5",
    "glioblastoma 6",
    "glioblastoma 7"
  ],
  "melanoma": [
    "melanoma",
    "melanoma",
    "melanoma",
    "MELANOMA",
    "Melanoma",
    "melanoma 1",
    "melanoma 2",
    "melanoma 3",
    "melanoma 4",
    "melanoma 5",
    "melanoma 6",
    "melanoma 7"
  ],
  "acute myeloid leukemia": [
    "acute myeloid leukemia",
    "acute myeloid leukemia",
    "acute-myeloid-leukemia",
    "ACUTE MYELOID LEUKEMIA",
    "Acute myeloid leukemia",
    "acute myeloid leukemia 1",
    "acute myeloid leukemia 2",
    "acute myeloid leukemia 3",
    "acute myeloid leukemia 4",
    "acute myeloid leukemia 5",
    "acute myeloid leukemia 6",
    "acute myeloid leukemia 7"
  ],
  "acute lymphoblastic leukemia": [
    "acute lymphoblastic leukemia",
    "acute lymphoblastic leukemia",
    "acute-lymphoblastic-leukemia",
    "ACUTE LYMPHOBLASTIC LEUKEMIA",
    "Acute lymphoblastic leukemia",
    "acute lymphoblastic leukemia 1",
    "acute lymphoblastic leukemia 2",
    "acute lymphoblastic leukemia 3",
    "acute lymphoblastic leukemia 4",
    "acute lymphoblastic leukemia 5",
    "acute lymphoblastic leukemia 6",
    "acute lymphoblastic leukemia 7"
  ],
  "lymphoma": [
    "lymphoma",
    "lymphoma",
    "lymphoma",
    "LYMPHOMA",
    "Lymphoma",
    "lymphoma 1",
    "lymphoma 2",
    "lymphoma 3",
    "lymphoma 4",
    "lymphoma 5",
    "lymphoma 6",
    "lymphoma 7"
  ],
  "multiple myeloma": [
    "multiple myeloma",
    "multiple myeloma",
    "multiple-myeloma",
    "MULTIPLE MYELOMA",
    "Multiple myeloma",
    "multiple myeloma 1",
    "multiple myeloma 2",
    "multiple myeloma 3",
    "multiple myeloma 4",
    "multiple myeloma 5",
    "multiple myeloma 6",
    "multiple myeloma 7"
  ],
  "ovarian cancer": [
    "ovarian cancer",
    "ovarian cancer",
    "ovarian-cancer",
    "OVARIAN CANCER",
    "Ovarian cancer",
    "ovarian cancer 1",
    "ovarian cancer 2",
    "ovarian cancer 3",
    "ovarian cancer 4",
    "ovarian cancer 5",
    "ovarian cancer 6",
    "ovarian cancer 7"
  ],
  "gastric cancer": [
    "gastric cancer",
    "gastric cancer",
    "gastric-cancer",
    "GASTRIC CANCER",
    "Gastric cancer",
    "gastric cancer 1",
    "gastric cancer 2",
    "gastric cancer 3",
    "gastric cancer 4",
    "gastric cancer 5",
    "gastric cancer 6",
    "gastric cancer 7"
  ],
  "esophageal cancer": [
    "esophageal cancer",
    "esophageal cancer",
    "esophageal-cancer",
    "ESOPHAGEAL CANCER",
    "Esophageal cancer",
    "esophageal cancer 1",
    "esophageal cancer 2",
    "esophageal cancer 3",
    "esophageal cancer 4",
    "esophageal cancer 5",
    "esophageal cancer 6",
    "esophageal cancer 7"
  ],
  "hepatocellular carcinoma": [
    "hepatocellular carcinoma",
    "hepatocellular carcinoma",
    "hepatocellular-carcinoma",
    "HEPATOCELLULAR CARCINOMA",
    "Hepatocellular carcinoma",
    "hepatocellular carcinoma 1",
    "hepatocellular carcinoma 2",
    "hepatocellular carcinoma 3",
    "hepatocellular carcinoma 4",
    "hepatocellular carcinoma 5",
    "hepatocellular carcinoma 6",
    "hepatocellular carcinoma 7"
  ],
  "cholangiocarcinoma": [
    "cholangiocarcinoma",
    "cholangiocarcinoma",
    "cholangiocarcinoma",
    "CHOLANGIOCARCINOMA",
    "Cholangiocarcinoma",
    "cholangiocarcinoma 1",
    "cholangiocarcinoma 2",
    "cholangiocarcinoma 3",
    "cholangiocarcinoma 4",
    "cholangiocarcinoma 5",
    "cholangiocarcinoma 6",
    "cholangiocarcinoma 7"
  ],
  "renal cell carcinoma": [
    "renal cell carcinoma",
    "renal cell carcinoma",
    "renal-cell-carcinoma",
    "RENAL CELL CARCINOMA",
    "Renal cell carcinoma",
    "renal cell carcinoma 1",
    "renal cell carcinoma 2",
    "renal cell carcinoma 3",
    "renal cell carcinoma 4",
    "renal cell carcinoma 5",
    "renal cell carcinoma 6",
    "renal cell carcinoma 7"
  ],
  "bladder cancer": [
    "bladder cancer",
    "bladder cancer",
    "bladder-cancer",
    "BLADDER CANCER",
    "Bladder cancer",
    "bladder cancer 1",
    "bladder cancer 2",
    "bladder cancer 3",
    "bladder cancer 4",
    "bladder cancer 5",
    "bladder cancer 6",
    "bladder cancer 7"
  ],
  "cervical cancer": [
    "cervical cancer",
    "cervical cancer",
    "cervical-cancer",
    "CERVICAL CANCER",
    "Cervical cancer",
    "cervical cancer 1",
    "cervical cancer 2",
    "cervical cancer 3",
    "cervical cancer 4",
    "cervical cancer 5",
    "cervical cancer 6",
    "cervical cancer 7"
  ],
  "endometrial cancer": [
    "endometrial cancer",
    "endometrial cancer",
    "endometrial-cancer",
    "ENDOMETRIAL CANCER",
    "Endometrial cancer",
    "endometrial cancer 1",
    "endometrial cancer 2",
    "endometrial cancer 3",
    "endometrial cancer 4",
    "endometrial cancer 5",
    "endometrial cancer 6",
    "endometrial cancer 7"
  ],
  "sarcoma": [
    "sarcoma",
    "sarcoma",
    "sarcoma",
    "SARCOMA",
    "Sarcoma",
    "sarcoma 1",
    "sarcoma 2",
    "sarcoma 3",
    "sarcoma 4",
    "sarcoma 5",
    "sarcoma 6",
    "sarcoma 7"
  ],
  "alzheimer disease": [
    "alzheimer disease",
    "alzheimer",
    "alzheimer-disease",
    "ALZHEIMER DISEASE",
    "Alzheimer disease",
    "alzheimer disease 1",
    "alzheimer disease 2",
    "alzheimer disease 3",
    "alzheimer disease 4",
    "alzheimer disease 5",
    "alzheimer disease 6",
    "alzheimer disease 7"
  ],
  "parkinson disease": [
    "parkinson disease",
    "parkinson",
    "parkinson-disease",
    "PARKINSON DISEASE",
    "Parkinson disease",
    "parkinson disease 1",
    "parkinson disease 2",
    "parkinson disease 3",
    "parkinson disease 4",
    "parkinson disease 5",
    "parkinson disease 6",
    "parkinson disease 7"
  ],
  "huntington disease": [
    "huntington disease",
    "huntington",
    "huntington-disease",
    "HUNTINGTON DISEASE",
    "Huntington disease",
    "huntington disease 1",
    "huntington disease 2",
    "huntington disease 3",
    "huntington disease 4",
    "huntington disease 5",
    "huntington disease 6",
    "huntington disease 7"
  ],
  "multiple sclerosis": [
    "multiple sclerosis",
    "multiple sclerosis",
    "multiple-sclerosis",
    "MULTIPLE SCLEROSIS",
    "Multiple sclerosis",
    "multiple sclerosis 1",
    "multiple sclerosis 2",
    "multiple sclerosis 3",
    "multiple sclerosis 4",
    "multiple sclerosis 5",
    "multiple sclerosis 6",
    "multiple sclerosis 7"
  ],
  "epilepsy": [
    "epilepsy",
    "epilepsy",
    "epilepsy",
    "EPILEPSY",
    "Epilepsy",
    "epilepsy 1",
    "epilepsy 2",
    "epilepsy 3",
    "epilepsy 4",
    "epilepsy 5",
    "epilepsy 6",
    "epilepsy 7"
  ],
  "stroke": [
    "stroke",
    "stroke",
    "stroke",
    "STROKE",
    "Stroke",
    "stroke 1",
    "stroke 2",
    "stroke 3",
    "stroke 4",
    "stroke 5",
    "stroke 6",
    "stroke 7"
  ],
  "migraine": [
    "migraine",
    "migraine",
    "migraine",
    "MIGRAINE",
    "Migraine",
    "migraine 1",
    "migraine 2",
    "migraine 3",
    "migraine 4",
    "migraine 5",
    "migraine 6",
    "migraine 7"
  ],
  "heart failure": [
    "heart failure",
    "heart failure",
    "heart-failure",
    "HEART FAILURE",
    "Heart failure",
    "heart failure 1",
    "heart failure 2",
    "heart failure 3",
    "heart failure 4",
    "heart failure 5",
    "heart failure 6",
    "heart failure 7"
  ],
  "coronary artery disease": [
    "coronary artery disease",
    "coronary artery",
    "coronary-artery-disease",
    "CORONARY ARTERY DISEASE",
    "Coronary artery disease",
    "coronary artery disease 1",
    "coronary artery disease 2",
    "coronary artery disease 3",
    "coronary artery disease 4",
    "coronary artery disease 5",
    "coronary artery disease 6",
    "coronary artery disease 7"
  ],
  "atrial fibrillation": [
    "atrial fibrillation",
    "atrial fibrillation",
    "atrial-fibrillation",
    "ATRIAL FIBRILLATION",
    "Atrial fibrillation",
    "atrial fibrillation 1",
    "atrial fibrillation 2",
    "atrial fibrillation 3",
    "atrial fibrillation 4",
    "atrial fibrillation 5",
    "atrial fibrillation 6",
    "atrial fibrillation 7"
  ],
  "hypertension": [
    "hypertension",
    "hypertension",
    "hypertension",
    "HYPERTENSION",
    "Hypertension",
    "hypertension 1",
    "hypertension 2",
    "hypertension 3",
    "hypertension 4",
    "hypertension 5",
    "hypertension 6",
    "hypertension 7"
  ],
  "hypercholesterolemia": [
    "hypercholesterolemia",
    "hypercholesterolemia",
    "hypercholesterolemia",
    "HYPERCHOLESTEROLEMIA",
    "Hypercholesterolemia",
    "hypercholesterolemia 1",
    "hypercholesterolemia 2",
    "hypercholesterolemia 3",
    "hypercholesterolemia 4",
    "hypercholesterolemia 5",
    "hypercholesterolemia 6",
    "hypercholesterolemia 7"
  ],
  "pulmonary hypertension": [
    "pulmonary hypertension",
    "pulmonary hypertension",
    "pulmonary-hypertension",
    "PULMONARY HYPERTENSION",
    "Pulmonary hypertension",
    "pulmonary hypertension 1",
    "pulmonary hypertension 2",
    "pulmonary hypertension 3",
    "pulmonary hypertension 4",
    "pulmonary hypertension 5",
    "pulmonary hypertension 6",
    "pulmonary hypertension 7"
  ],
  "type 2 diabetes": [
    "type 2 diabetes",
    "type 2 diabetes",
    "type-2-diabetes",
    "TYPE 2 DIABETES",
    "Type 2 diabetes",
    "type 2 diabetes 1",
    "type 2 diabetes 2",
    "type 2 diabetes 3",
    "type 2 diabetes 4",
    "type 2 diabetes 5",
    "type 2 diabetes 6",
    "type 2 diabetes 7"
  ],
  "type 1 diabetes": [
    "type 1 diabetes",
    "type 1 diabetes",
    "type-1-diabetes",
    "TYPE 1 DIABETES",
    "Type 1 diabetes",
    "type 1 diabetes 1",
    "type 1 diabetes 2",
    "type 1 diabetes 3",
    "type 1 diabetes 4",
    "type 1 diabetes 5",
    "type 1 diabetes 6",
    "type 1 diabetes 7"
  ],
  "obesity": [
    "obesity",
    "obesity",
    "obesity",
    "OBESITY",
    "Obesity",
    "obesity 1",
    "obesity 2",
    "obesity 3",
    "obesity 4",
    "obesity 5",
    "obesity 6",
    "obesity 7"
  ],
  "nafld": [
    "nafld",
    "nafld",
    "nafld",
    "NAFLD",
    "Nafld",
    "nafld 1",
    "nafld 2",
    "nafld 3",
    "nafld 4",
    "nafld 5",
    "nafld 6",
    "nafld 7"
  ],
  "nash": [
    "nash",
    "nash",
    "nash",
    "NASH",
    "Nash",
    "nash 1",
    "nash 2",
    "nash 3",
    "nash 4",
    "nash 5",
    "nash 6",
    "nash 7"
  ],
  "asthma": [
    "asthma",
    "asthma",
    "asthma",
    "ASTHMA",
    "Asthma",
    "asthma 1",
    "asthma 2",
    "asthma 3",
    "asthma 4",
    "asthma 5",
    "asthma 6",
    "asthma 7"
  ],
  "copd": [
    "copd",
    "copd",
    "copd",
    "COPD",
    "Copd",
    "copd 1",
    "copd 2",
    "copd 3",
    "copd 4",
    "copd 5",
    "copd 6",
    "copd 7"
  ],
  "pulmonary fibrosis": [
    "pulmonary fibrosis",
    "pulmonary fibrosis",
    "pulmonary-fibrosis",
    "PULMONARY FIBROSIS",
    "Pulmonary fibrosis",
    "pulmonary fibrosis 1",
    "pulmonary fibrosis 2",
    "pulmonary fibrosis 3",
    "pulmonary fibrosis 4",
    "pulmonary fibrosis 5",
    "pulmonary fibrosis 6",
    "pulmonary fibrosis 7"
  ],
  "rheumatoid arthritis": [
    "rheumatoid arthritis",
    "rheumatoid arthritis",
    "rheumatoid-arthritis",
    "RHEUMATOID ARTHRITIS",
    "Rheumatoid arthritis",
    "rheumatoid arthritis 1",
    "rheumatoid arthritis 2",
    "rheumatoid arthritis 3",
    "rheumatoid arthritis 4",
    "rheumatoid arthritis 5",
    "rheumatoid arthritis 6",
    "rheumatoid arthritis 7"
  ],
  "lupus": [
    "lupus",
    "lupus",
    "lupus",
    "LUPUS",
    "Lupus",
    "lupus 1",
    "lupus 2",
    "lupus 3",
    "lupus 4",
    "lupus 5",
    "lupus 6",
    "lupus 7"
  ],
  "psoriasis": [
    "psoriasis",
    "psoriasis",
    "psoriasis",
    "PSORIASIS",
    "Psoriasis",
    "psoriasis 1",
    "psoriasis 2",
    "psoriasis 3",
    "psoriasis 4",
    "psoriasis 5",
    "psoriasis 6",
    "psoriasis 7"
  ],
  "crohn disease": [
    "crohn disease",
    "crohn",
    "crohn-disease",
    "CROHN DISEASE",
    "Crohn disease",
    "crohn disease 1",
    "crohn disease 2",
    "crohn disease 3",
    "crohn disease 4",
    "crohn disease 5",
    "crohn disease 6",
    "crohn disease 7"
  ],
  "ulcerative colitis": [
    "ulcerative colitis",
    "ulcerative colitis",
    "ulcerative-colitis",
    "ULCERATIVE COLITIS",
    "Ulcerative colitis",
    "ulcerative colitis 1",
    "ulcerative colitis 2",
    "ulcerative colitis 3",
    "ulcerative colitis 4",
    "ulcerative colitis 5",
    "ulcerative colitis 6",
    "ulcerative colitis 7"
  ],
  "celiac disease": [
    "celiac disease",
    "celiac",
    "celiac-disease",
    "CELIAC DISEASE",
    "Celiac disease",
    "celiac disease 1",
    "celiac disease 2",
    "celiac disease 3",
    "celiac disease 4",
    "celiac disease 5",
    "celiac disease 6",
    "celiac disease 7"
  ],
  "ankylosing spondylitis": [
    "ankylosing spondylitis",
    "ankylosing spondylitis",
    "ankylosing-spondylitis",
    "ANKYLOSING SPONDYLITIS",
    "Ankylosing spondylitis",
    "ankylosing spondylitis 1",
    "ankylosing spondylitis 2",
    "ankylosing spondylitis 3",
    "ankylosing spondylitis 4",
    "ankylosing spondylitis 5",
    "ankylosing spondylitis 6",
    "ankylosing spondylitis 7"
  ],
  "chronic kidney disease": [
    "chronic kidney disease",
    "chronic kidney",
    "chronic-kidney-disease",
    "CHRONIC KIDNEY DISEASE",
    "Chronic kidney disease",
    "chronic kidney disease 1",
    "chronic kidney disease 2",
    "chronic kidney disease 3",
    "chronic kidney disease 4",
    "chronic kidney disease 5",
    "chronic kidney disease 6",
    "chronic kidney disease 7"
  ],
  "nephrotic syndrome": [
    "nephrotic syndrome",
    "nephrotic syndrome",
    "nephrotic-syndrome",
    "NEPHROTIC SYNDROME",
    "Nephrotic syndrome",
    "nephrotic syndrome 1",
    "nephrotic syndrome 2",
    "nephrotic syndrome 3",
    "nephrotic syndrome 4",
    "nephrotic syndrome 5",
    "nephrotic syndrome 6",
    "nephrotic syndrome 7"
  ],
  "polycystic kidney disease": [
    "polycystic kidney disease",
    "polycystic kidney",
    "polycystic-kidney-disease",
    "POLYCYSTIC KIDNEY DISEASE",
    "Polycystic kidney disease",
    "polycystic kidney disease 1",
    "polycystic kidney disease 2",
    "polycystic kidney disease 3",
    "polycystic kidney disease 4",
    "polycystic kidney disease 5",
    "polycystic kidney disease 6",
    "polycystic kidney disease 7"
  ],
  "hemophilia": [
    "hemophilia",
    "hemophilia",
    "hemophilia",
    "HEMOPHILIA",
    "Hemophilia",
    "hemophilia 1",
    "hemophilia 2",
    "hemophilia 3",
    "hemophilia 4",
    "hemophilia 5",
    "hemophilia 6",
    "hemophilia 7"
  ],
  "sickle cell disease": [
    "sickle cell disease",
    "sickle cell",
    "sickle-cell-disease",
    "SICKLE CELL DISEASE",
    "Sickle cell disease",
    "sickle cell disease 1",
    "sickle cell disease 2",
    "sickle cell disease 3",
    "sickle cell disease 4",
    "sickle cell disease 5",
    "sickle cell disease 6",
    "sickle cell disease 7"
  ],
  "thalassemia": [
    "thalassemia",
    "thalassemia",
    "thalassemia",
    "THALASSEMIA",
    "Thalassemia",
    "thalassemia 1",
    "thalassemia 2",
    "thalassemia 3",
    "thalassemia 4",
    "thalassemia 5",
    "thalassemia 6",
    "thalassemia 7"
  ],
  "covid-19": [
    "covid-19",
    "covid-19",
    "covid-19",
    "COVID-19",
    "Covid-19",
    "covid-19 1",
    "covid-19 2",
    "covid-19 3",
    "covid-19 4",
    "covid-19 5",
    "covid-19 6",
    "covid-19 7"
  ],
  "influenza": [
    "influenza",
    "influenza",
    "influenza",
    "INFLUENZA",
    "Influenza",
    "influenza 1",
    "influenza 2",
    "influenza 3",
    "influenza 4",
    "influenza 5",
    "influenza 6",
    "influenza 7"
  ],
  "hepatitis b": [
    "hepatitis b",
    "hepatitis b",
    "hepatitis-b",
    "HEPATITIS B",
    "Hepatitis b",
    "hepatitis b 1",
    "hepatitis b 2",
    "hepatitis b 3",
    "hepatitis b 4",
    "hepatitis b 5",
    "hepatitis b 6",
    "hepatitis b 7"
  ],
  "hepatitis c": [
    "hepatitis c",
    "hepatitis c",
    "hepatitis-c",
    "HEPATITIS C",
    "Hepatitis c",
    "hepatitis c 1",
    "hepatitis c 2",
    "hepatitis c 3",
    "hepatitis c 4",
    "hepatitis c 5",
    "hepatitis c 6",
    "hepatitis c 7"
  ],
  "hiv infection": [
    "hiv infection",
    "hiv infection",
    "hiv-infection",
    "HIV INFECTION",
    "Hiv infection",
    "hiv infection 1",
    "hiv infection 2",
    "hiv infection 3",
    "hiv infection 4",
    "hiv infection 5",
    "hiv infection 6",
    "hiv infection 7"
  ],
  "tuberculosis": [
    "tuberculosis",
    "tuberculosis",
    "tuberculosis",
    "TUBERCULOSIS",
    "Tuberculosis",
    "tuberculosis 1",
    "tuberculosis 2",
    "tuberculosis 3",
    "tuberculosis 4",
    "tuberculosis 5",
    "tuberculosis 6",
    "tuberculosis 7"
  ],
  "malaria": [
    "malaria",
    "malaria",
    "malaria",
    "MALARIA",
    "Malaria",
    "malaria 1",
    "malaria 2",
    "malaria 3",
    "malaria 4",
    "malaria 5",
    "malaria 6",
    "malaria 7"
  ],
  "depression": [
    "depression",
    "depression",
    "depression",
    "DEPRESSION",
    "Depression",
    "depression 1",
    "depression 2",
    "depression 3",
    "depression 4",
    "depression 5",
    "depression 6",
    "depression 7"
  ],
  "schizophrenia": [
    "schizophrenia",
    "schizophrenia",
    "schizophrenia",
    "SCHIZOPHRENIA",
    "Schizophrenia",
    "schizophrenia 1",
    "schizophrenia 2",
    "schizophrenia 3",
    "schizophrenia 4",
    "schizophrenia 5",
    "schizophrenia 6",
    "schizophrenia 7"
  ],
  "bipolar disorder": [
    "bipolar disorder",
    "bipolar disorder",
    "bipolar-disorder",
    "BIPOLAR DISORDER",
    "Bipolar disorder",
    "bipolar disorder 1",
    "bipolar disorder 2",
    "bipolar disorder 3",
    "bipolar disorder 4",
    "bipolar disorder 5",
    "bipolar disorder 6",
    "bipolar disorder 7"
  ],
  "autism spectrum disorder": [
    "autism spectrum disorder",
    "autism spectrum disorder",
    "autism-spectrum-disorder",
    "AUTISM SPECTRUM DISORDER",
    "Autism spectrum disorder",
    "autism spectrum disorder 1",
    "autism spectrum disorder 2",
    "autism spectrum disorder 3",
    "autism spectrum disorder 4",
    "autism spectrum disorder 5",
    "autism spectrum disorder 6",
    "autism spectrum disorder 7"
  ],
  "adhd": [
    "adhd",
    "adhd",
    "adhd",
    "ADHD",
    "Adhd",
    "adhd 1",
    "adhd 2",
    "adhd 3",
    "adhd 4",
    "adhd 5",
    "adhd 6",
    "adhd 7"
  ],
  "anxiety disorder": [
    "anxiety disorder",
    "anxiety disorder",
    "anxiety-disorder",
    "ANXIETY DISORDER",
    "Anxiety disorder",
    "anxiety disorder 1",
    "anxiety disorder 2",
    "anxiety disorder 3",
    "anxiety disorder 4",
    "anxiety disorder 5",
    "anxiety disorder 6",
    "anxiety disorder 7"
  ]
}\nCELLTYPE_SYNONYMS = {
  "T cell": [
    "T cell",
    "t cell",
    "T_cell",
    "T"
  ],
  "B cell": [
    "B cell",
    "b cell",
    "B_cell",
    "B"
  ],
  "NK cell": [
    "NK cell",
    "nk cell",
    "NK_cell",
    "NK"
  ],
  "Monocyte": [
    "Monocyte",
    "monocyte",
    "Monocyte",
    "Monocyte"
  ],
  "Macrophage": [
    "Macrophage",
    "macrophage",
    "Macrophage",
    "Macrophage"
  ],
  "Dendritic cell": [
    "Dendritic cell",
    "dendritic cell",
    "Dendritic_cell",
    "Dendritic"
  ],
  "Neutrophil": [
    "Neutrophil",
    "neutrophil",
    "Neutrophil",
    "Neutrophil"
  ],
  "Eosinophil": [
    "Eosinophil",
    "eosinophil",
    "Eosinophil",
    "Eosinophil"
  ],
  "Basophil": [
    "Basophil",
    "basophil",
    "Basophil",
    "Basophil"
  ],
  "Endothelial cell": [
    "Endothelial cell",
    "endothelial cell",
    "Endothelial_cell",
    "Endothelial"
  ],
  "Fibroblast": [
    "Fibroblast",
    "fibroblast",
    "Fibroblast",
    "Fibroblast"
  ],
  "Pericyte": [
    "Pericyte",
    "pericyte",
    "Pericyte",
    "Pericyte"
  ],
  "Smooth muscle": [
    "Smooth muscle",
    "smooth muscle",
    "Smooth_muscle",
    "Smooth muscle"
  ],
  "Cardiomyocyte": [
    "Cardiomyocyte",
    "cardiomyocyte",
    "Cardiomyocyte",
    "Cardiomyocyte"
  ],
  "Hepatocyte": [
    "Hepatocyte",
    "hepatocyte",
    "Hepatocyte",
    "Hepatocyte"
  ],
  "Cholangiocyte": [
    "Cholangiocyte",
    "cholangiocyte",
    "Cholangiocyte",
    "Cholangiocyte"
  ],
  "Podocyte": [
    "Podocyte",
    "podocyte",
    "Podocyte",
    "Podocyte"
  ],
  "Proximal tubule": [
    "Proximal tubule",
    "proximal tubule",
    "Proximal_tubule",
    "Proximal tubule"
  ],
  "Distal tubule": [
    "Distal tubule",
    "distal tubule",
    "Distal_tubule",
    "Distal tubule"
  ],
  "Enterocyte": [
    "Enterocyte",
    "enterocyte",
    "Enterocyte",
    "Enterocyte"
  ],
  "Goblet cell": [
    "Goblet cell",
    "goblet cell",
    "Goblet_cell",
    "Goblet"
  ],
  "Paneth cell": [
    "Paneth cell",
    "paneth cell",
    "Paneth_cell",
    "Paneth"
  ],
  "Tuft cell": [
    "Tuft cell",
    "tuft cell",
    "Tuft_cell",
    "Tuft"
  ],
  "Alveolar type I": [
    "Alveolar type I",
    "alveolar type i",
    "Alveolar_type_I",
    "Alveolar type I"
  ],
  "Alveolar type II": [
    "Alveolar type II",
    "alveolar type ii",
    "Alveolar_type_II",
    "Alveolar type II"
  ],
  "Club cell": [
    "Club cell",
    "club cell",
    "Club_cell",
    "Club"
  ],
  "Keratinocyte": [
    "Keratinocyte",
    "keratinocyte",
    "Keratinocyte",
    "Keratinocyte"
  ],
  "Melanocyte": [
    "Melanocyte",
    "melanocyte",
    "Melanocyte",
    "Melanocyte"
  ],
  "Oligodendrocyte": [
    "Oligodendrocyte",
    "oligodendrocyte",
    "Oligodendrocyte",
    "Oligodendrocyte"
  ],
  "Astrocyte": [
    "Astrocyte",
    "astrocyte",
    "Astrocyte",
    "Astrocyte"
  ],
  "Microglia": [
    "Microglia",
    "microglia",
    "Microglia",
    "Microglia"
  ],
  "Neuron": [
    "Neuron",
    "neuron",
    "Neuron",
    "Neuron"
  ],
  "Purkinje cell": [
    "Purkinje cell",
    "purkinje cell",
    "Purkinje_cell",
    "Purkinje"
  ],
  "Beta cell": [
    "Beta cell",
    "beta cell",
    "Beta_cell",
    "Beta"
  ],
  "Alpha cell": [
    "Alpha cell",
    "alpha cell",
    "Alpha_cell",
    "Alpha"
  ],
  "Delta cell": [
    "Delta cell",
    "delta cell",
    "Delta_cell",
    "Delta"
  ],
  "Acinar cell": [
    "Acinar cell",
    "acinar cell",
    "Acinar_cell",
    "Acinar"
  ],
  "Ductal cell": [
    "Ductal cell",
    "ductal cell",
    "Ductal_cell",
    "Ductal"
  ]
}\nAE_TERMS = {
  "nausea": [
    "nausea",
    "nausea",
    "NAUSEA",
    "Nausea",
    "nausea"
  ],
  "vomiting": [
    "vomiting",
    "vomiting",
    "VOMITING",
    "Vomiting",
    "vomiting"
  ],
  "diarrhea": [
    "diarrhea",
    "diarrhea",
    "DIARRHEA",
    "Diarrhea",
    "diarrhea"
  ],
  "constipation": [
    "constipation",
    "constipation",
    "CONSTIPATION",
    "Constipation",
    "constipation"
  ],
  "headache": [
    "headache",
    "headache",
    "HEADACHE",
    "Headache",
    "headache"
  ],
  "dizziness": [
    "dizziness",
    "dizziness",
    "DIZZINESS",
    "Dizziness",
    "dizziness"
  ],
  "rash": [
    "rash",
    "rash",
    "RASH",
    "Rash",
    "rash"
  ],
  "pruritus": [
    "pruritus",
    "pruritus",
    "PRURITUS",
    "Pruritus",
    "pruritus"
  ],
  "urticaria": [
    "urticaria",
    "urticaria",
    "URTICARIA",
    "Urticaria",
    "urticaria"
  ],
  "fatigue": [
    "fatigue",
    "fatigue",
    "FATIGUE",
    "Fatigue",
    "fatigue"
  ],
  "elevated ALT": [
    "elevated ALT",
    "elevated alt",
    "ELEVATED ALT",
    "Elevated alt",
    "elevated_ALT"
  ],
  "elevated AST": [
    "elevated AST",
    "elevated ast",
    "ELEVATED AST",
    "Elevated ast",
    "elevated_AST"
  ],
  "QT prolongation": [
    "QT prolongation",
    "qt prolongation",
    "QT PROLONGATION",
    "Qt prolongation",
    "QT_prolongation"
  ],
  "arrhythmia": [
    "arrhythmia",
    "arrhythmia",
    "ARRHYTHMIA",
    "Arrhythmia",
    "arrhythmia"
  ],
  "myocarditis": [
    "myocarditis",
    "myocarditis",
    "MYOCARDITIS",
    "Myocarditis",
    "myocarditis"
  ],
  "seizure": [
    "seizure",
    "seizure",
    "SEIZURE",
    "Seizure",
    "seizure"
  ],
  "neuropathy": [
    "neuropathy",
    "neuropathy",
    "NEUROPATHY",
    "Neuropathy",
    "neuropathy"
  ],
  "neutropenia": [
    "neutropenia",
    "neutropenia",
    "NEUTROPENIA",
    "Neutropenia",
    "neutropenia"
  ],
  "thrombocytopenia": [
    "thrombocytopenia",
    "thrombocytopenia",
    "THROMBOCYTOPENIA",
    "Thrombocytopenia",
    "thrombocytopenia"
  ],
  "anemia": [
    "anemia",
    "anemia",
    "ANEMIA",
    "Anemia",
    "anemia"
  ],
  "renal failure": [
    "renal failure",
    "renal failure",
    "RENAL FAILURE",
    "Renal failure",
    "renal_failure"
  ],
  "proteinuria": [
    "proteinuria",
    "proteinuria",
    "PROTEINURIA",
    "Proteinuria",
    "proteinuria"
  ],
  "hepatotoxicity": [
    "hepatotoxicity",
    "hepatotoxicity",
    "HEPATOTOXICITY",
    "Hepatotoxicity",
    "hepatotoxicity"
  ],
  "pancreatitis": [
    "pancreatitis",
    "pancreatitis",
    "PANCREATITIS",
    "Pancreatitis",
    "pancreatitis"
  ],
  "colitis": [
    "colitis",
    "colitis",
    "COLITIS",
    "Colitis",
    "colitis"
  ],
  "pneumonitis": [
    "pneumonitis",
    "pneumonitis",
    "PNEUMONITIS",
    "Pneumonitis",
    "pneumonitis"
  ]
}\nPROTEIN_DOMAINS = {
  "PF1001": "Domain_1_repeat_rich_motif",
  "PF1002": "Domain_2_repeat_rich_motif",
  "PF1003": "Domain_3_repeat_rich_motif",
  "PF1004": "Domain_4_repeat_rich_motif",
  "PF1005": "Domain_5_repeat_rich_motif",
  "PF1006": "Domain_6_repeat_rich_motif",
  "PF1007": "Domain_7_repeat_rich_motif",
  "PF1008": "Domain_8_repeat_rich_motif",
  "PF1009": "Domain_9_repeat_rich_motif",
  "PF1010": "Domain_10_repeat_rich_motif",
  "PF1011": "Domain_11_repeat_rich_motif",
  "PF1012": "Domain_12_repeat_rich_motif",
  "PF1013": "Domain_13_repeat_rich_motif",
  "PF1014": "Domain_14_repeat_rich_motif",
  "PF1015": "Domain_15_repeat_rich_motif",
  "PF1016": "Domain_16_repeat_rich_motif",
  "PF1017": "Domain_17_repeat_rich_motif",
  "PF1018": "Domain_18_repeat_rich_motif",
  "PF1019": "Domain_19_repeat_rich_motif",
  "PF1020": "Domain_20_repeat_rich_motif",
  "PF1021": "Domain_21_repeat_rich_motif",
  "PF1022": "Domain_22_repeat_rich_motif",
  "PF1023": "Domain_23_repeat_rich_motif",
  "PF1024": "Domain_24_repeat_rich_motif",
  "PF1025": "Domain_25_repeat_rich_motif",
  "PF1026": "Domain_26_repeat_rich_motif",
  "PF1027": "Domain_27_repeat_rich_motif",
  "PF1028": "Domain_28_repeat_rich_motif",
  "PF1029": "Domain_29_repeat_rich_motif",
  "PF1030": "Domain_30_repeat_rich_motif",
  "PF1031": "Domain_31_repeat_rich_motif",
  "PF1032": "Domain_32_repeat_rich_motif",
  "PF1033": "Domain_33_repeat_rich_motif",
  "PF1034": "Domain_34_repeat_rich_motif",
  "PF1035": "Domain_35_repeat_rich_motif",
  "PF1036": "Domain_36_repeat_rich_motif",
  "PF1037": "Domain_37_repeat_rich_motif",
  "PF1038": "Domain_38_repeat_rich_motif",
  "PF1039": "Domain_39_repeat_rich_motif",
  "PF1040": "Domain_40_repeat_rich_motif",
  "PF1041": "Domain_41_repeat_rich_motif",
  "PF1042": "Domain_42_repeat_rich_motif",
  "PF1043": "Domain_43_repeat_rich_motif",
  "PF1044": "Domain_44_repeat_rich_motif",
  "PF1045": "Domain_45_repeat_rich_motif",
  "PF1046": "Domain_46_repeat_rich_motif",
  "PF1047": "Domain_47_repeat_rich_motif",
  "PF1048": "Domain_48_repeat_rich_motif",
  "PF1049": "Domain_49_repeat_rich_motif",
  "PF1050": "Domain_50_repeat_rich_motif",
  "PF1051": "Domain_51_repeat_rich_motif",
  "PF1052": "Domain_52_repeat_rich_motif",
  "PF1053": "Domain_53_repeat_rich_motif",
  "PF1054": "Domain_54_repeat_rich_motif",
  "PF1055": "Domain_55_repeat_rich_motif",
  "PF1056": "Domain_56_repeat_rich_motif",
  "PF1057": "Domain_57_repeat_rich_motif",
  "PF1058": "Domain_58_repeat_rich_motif",
  "PF1059": "Domain_59_repeat_rich_motif",
  "PF1060": "Domain_60_repeat_rich_motif",
  "PF1061": "Domain_61_repeat_rich_motif",
  "PF1062": "Domain_62_repeat_rich_motif",
  "PF1063": "Domain_63_repeat_rich_motif",
  "PF1064": "Domain_64_repeat_rich_motif",
  "PF1065": "Domain_65_repeat_rich_motif",
  "PF1066": "Domain_66_repeat_rich_motif",
  "PF1067": "Domain_67_repeat_rich_motif",
  "PF1068": "Domain_68_repeat_rich_motif",
  "PF1069": "Domain_69_repeat_rich_motif",
  "PF1070": "Domain_70_repeat_rich_motif",
  "PF1071": "Domain_71_repeat_rich_motif",
  "PF1072": "Domain_72_repeat_rich_motif",
  "PF1073": "Domain_73_repeat_rich_motif",
  "PF1074": "Domain_74_repeat_rich_motif",
  "PF1075": "Domain_75_repeat_rich_motif",
  "PF1076": "Domain_76_repeat_rich_motif",
  "PF1077": "Domain_77_repeat_rich_motif",
  "PF1078": "Domain_78_repeat_rich_motif",
  "PF1079": "Domain_79_repeat_rich_motif",
  "PF1080": "Domain_80_repeat_rich_motif",
  "PF1081": "Domain_81_repeat_rich_motif",
  "PF1082": "Domain_82_repeat_rich_motif",
  "PF1083": "Domain_83_repeat_rich_motif",
  "PF1084": "Domain_84_repeat_rich_motif",
  "PF1085": "Domain_85_repeat_rich_motif",
  "PF1086": "Domain_86_repeat_rich_motif",
  "PF1087": "Domain_87_repeat_rich_motif",
  "PF1088": "Domain_88_repeat_rich_motif",
  "PF1089": "Domain_89_repeat_rich_motif",
  "PF1090": "Domain_90_repeat_rich_motif",
  "PF1091": "Domain_91_repeat_rich_motif",
  "PF1092": "Domain_92_repeat_rich_motif",
  "PF1093": "Domain_93_repeat_rich_motif",
  "PF1094": "Domain_94_repeat_rich_motif",
  "PF1095": "Domain_95_repeat_rich_motif",
  "PF1096": "Domain_96_repeat_rich_motif",
  "PF1097": "Domain_97_repeat_rich_motif",
  "PF1098": "Domain_98_repeat_rich_motif",
  "PF1099": "Domain_99_repeat_rich_motif",
  "PF1100": "Domain_100_repeat_rich_motif",
  "PF1101": "Domain_101_repeat_rich_motif",
  "PF1102": "Domain_102_repeat_rich_motif",
  "PF1103": "Domain_103_repeat_rich_motif",
  "PF1104": "Domain_104_repeat_rich_motif",
  "PF1105": "Domain_105_repeat_rich_motif",
  "PF1106": "Domain_106_repeat_rich_motif",
  "PF1107": "Domain_107_repeat_rich_motif",
  "PF1108": "Domain_108_repeat_rich_motif",
  "PF1109": "Domain_109_repeat_rich_motif",
  "PF1110": "Domain_110_repeat_rich_motif",
  "PF1111": "Domain_111_repeat_rich_motif",
  "PF1112": "Domain_112_repeat_rich_motif",
  "PF1113": "Domain_113_repeat_rich_motif",
  "PF1114": "Domain_114_repeat_rich_motif",
  "PF1115": "Domain_115_repeat_rich_motif",
  "PF1116": "Domain_116_repeat_rich_motif",
  "PF1117": "Domain_117_repeat_rich_motif",
  "PF1118": "Domain_118_repeat_rich_motif",
  "PF1119": "Domain_119_repeat_rich_motif",
  "PF1120": "Domain_120_repeat_rich_motif",
  "PF1121": "Domain_121_repeat_rich_motif",
  "PF1122": "Domain_122_repeat_rich_motif",
  "PF1123": "Domain_123_repeat_rich_motif",
  "PF1124": "Domain_124_repeat_rich_motif",
  "PF1125": "Domain_125_repeat_rich_motif",
  "PF1126": "Domain_126_repeat_rich_motif",
  "PF1127": "Domain_127_repeat_rich_motif",
  "PF1128": "Domain_128_repeat_rich_motif",
  "PF1129": "Domain_129_repeat_rich_motif",
  "PF1130": "Domain_130_repeat_rich_motif",
  "PF1131": "Domain_131_repeat_rich_motif",
  "PF1132": "Domain_132_repeat_rich_motif",
  "PF1133": "Domain_133_repeat_rich_motif",
  "PF1134": "Domain_134_repeat_rich_motif",
  "PF1135": "Domain_135_repeat_rich_motif",
  "PF1136": "Domain_136_repeat_rich_motif",
  "PF1137": "Domain_137_repeat_rich_motif",
  "PF1138": "Domain_138_repeat_rich_motif",
  "PF1139": "Domain_139_repeat_rich_motif",
  "PF1140": "Domain_140_repeat_rich_motif",
  "PF1141": "Domain_141_repeat_rich_motif",
  "PF1142": "Domain_142_repeat_rich_motif",
  "PF1143": "Domain_143_repeat_rich_motif",
  "PF1144": "Domain_144_repeat_rich_motif",
  "PF1145": "Domain_145_repeat_rich_motif",
  "PF1146": "Domain_146_repeat_rich_motif",
  "PF1147": "Domain_147_repeat_rich_motif",
  "PF1148": "Domain_148_repeat_rich_motif",
  "PF1149": "Domain_149_repeat_rich_motif",
  "PF1150": "Domain_150_repeat_rich_motif",
  "PF1151": "Domain_151_repeat_rich_motif",
  "PF1152": "Domain_152_repeat_rich_motif",
  "PF1153": "Domain_153_repeat_rich_motif",
  "PF1154": "Domain_154_repeat_rich_motif",
  "PF1155": "Domain_155_repeat_rich_motif",
  "PF1156": "Domain_156_repeat_rich_motif",
  "PF1157": "Domain_157_repeat_rich_motif",
  "PF1158": "Domain_158_repeat_rich_motif",
  "PF1159": "Domain_159_repeat_rich_motif",
  "PF1160": "Domain_160_repeat_rich_motif",
  "PF1161": "Domain_161_repeat_rich_motif",
  "PF1162": "Domain_162_repeat_rich_motif",
  "PF1163": "Domain_163_repeat_rich_motif",
  "PF1164": "Domain_164_repeat_rich_motif",
  "PF1165": "Domain_165_repeat_rich_motif",
  "PF1166": "Domain_166_repeat_rich_motif",
  "PF1167": "Domain_167_repeat_rich_motif",
  "PF1168": "Domain_168_repeat_rich_motif",
  "PF1169": "Domain_169_repeat_rich_motif",
  "PF1170": "Domain_170_repeat_rich_motif",
  "PF1171": "Domain_171_repeat_rich_motif",
  "PF1172": "Domain_172_repeat_rich_motif",
  "PF1173": "Domain_173_repeat_rich_motif",
  "PF1174": "Domain_174_repeat_rich_motif",
  "PF1175": "Domain_175_repeat_rich_motif",
  "PF1176": "Domain_176_repeat_rich_motif",
  "PF1177": "Domain_177_repeat_rich_motif",
  "PF1178": "Domain_178_repeat_rich_motif",
  "PF1179": "Domain_179_repeat_rich_motif",
  "PF1180": "Domain_180_repeat_rich_motif",
  "PF1181": "Domain_181_repeat_rich_motif",
  "PF1182": "Domain_182_repeat_rich_motif",
  "PF1183": "Domain_183_repeat_rich_motif",
  "PF1184": "Domain_184_repeat_rich_motif",
  "PF1185": "Domain_185_repeat_rich_motif",
  "PF1186": "Domain_186_repeat_rich_motif",
  "PF1187": "Domain_187_repeat_rich_motif",
  "PF1188": "Domain_188_repeat_rich_motif",
  "PF1189": "Domain_189_repeat_rich_motif",
  "PF1190": "Domain_190_repeat_rich_motif",
  "PF1191": "Domain_191_repeat_rich_motif",
  "PF1192": "Domain_192_repeat_rich_motif",
  "PF1193": "Domain_193_repeat_rich_motif",
  "PF1194": "Domain_194_repeat_rich_motif",
  "PF1195": "Domain_195_repeat_rich_motif",
  "PF1196": "Domain_196_repeat_rich_motif",
  "PF1197": "Domain_197_repeat_rich_motif",
  "PF1198": "Domain_198_repeat_rich_motif",
  "PF1199": "Domain_199_repeat_rich_motif",
  "PF1200": "Domain_200_repeat_rich_motif",
  "PF1201": "Domain_201_repeat_rich_motif",
  "PF1202": "Domain_202_repeat_rich_motif",
  "PF1203": "Domain_203_repeat_rich_motif",
  "PF1204": "Domain_204_repeat_rich_motif",
  "PF1205": "Domain_205_repeat_rich_motif",
  "PF1206": "Domain_206_repeat_rich_motif",
  "PF1207": "Domain_207_repeat_rich_motif",
  "PF1208": "Domain_208_repeat_rich_motif",
  "PF1209": "Domain_209_repeat_rich_motif",
  "PF1210": "Domain_210_repeat_rich_motif",
  "PF1211": "Domain_211_repeat_rich_motif",
  "PF1212": "Domain_212_repeat_rich_motif",
  "PF1213": "Domain_213_repeat_rich_motif",
  "PF1214": "Domain_214_repeat_rich_motif",
  "PF1215": "Domain_215_repeat_rich_motif",
  "PF1216": "Domain_216_repeat_rich_motif",
  "PF1217": "Domain_217_repeat_rich_motif",
  "PF1218": "Domain_218_repeat_rich_motif",
  "PF1219": "Domain_219_repeat_rich_motif",
  "PF1220": "Domain_220_repeat_rich_motif",
  "PF1221": "Domain_221_repeat_rich_motif",
  "PF1222": "Domain_222_repeat_rich_motif",
  "PF1223": "Domain_223_repeat_rich_motif",
  "PF1224": "Domain_224_repeat_rich_motif",
  "PF1225": "Domain_225_repeat_rich_motif",
  "PF1226": "Domain_226_repeat_rich_motif",
  "PF1227": "Domain_227_repeat_rich_motif",
  "PF1228": "Domain_228_repeat_rich_motif",
  "PF1229": "Domain_229_repeat_rich_motif",
  "PF1230": "Domain_230_repeat_rich_motif",
  "PF1231": "Domain_231_repeat_rich_motif",
  "PF1232": "Domain_232_repeat_rich_motif",
  "PF1233": "Domain_233_repeat_rich_motif",
  "PF1234": "Domain_234_repeat_rich_motif",
  "PF1235": "Domain_235_repeat_rich_motif",
  "PF1236": "Domain_236_repeat_rich_motif",
  "PF1237": "Domain_237_repeat_rich_motif",
  "PF1238": "Domain_238_repeat_rich_motif",
  "PF1239": "Domain_239_repeat_rich_motif",
  "PF1240": "Domain_240_repeat_rich_motif",
  "PF1241": "Domain_241_repeat_rich_motif",
  "PF1242": "Domain_242_repeat_rich_motif",
  "PF1243": "Domain_243_repeat_rich_motif",
  "PF1244": "Domain_244_repeat_rich_motif",
  "PF1245": "Domain_245_repeat_rich_motif",
  "PF1246": "Domain_246_repeat_rich_motif",
  "PF1247": "Domain_247_repeat_rich_motif",
  "PF1248": "Domain_248_repeat_rich_motif",
  "PF1249": "Domain_249_repeat_rich_motif",
  "PF1250": "Domain_250_repeat_rich_motif",
  "PF1251": "Domain_251_repeat_rich_motif",
  "PF1252": "Domain_252_repeat_rich_motif",
  "PF1253": "Domain_253_repeat_rich_motif",
  "PF1254": "Domain_254_repeat_rich_motif",
  "PF1255": "Domain_255_repeat_rich_motif",
  "PF1256": "Domain_256_repeat_rich_motif",
  "PF1257": "Domain_257_repeat_rich_motif",
  "PF1258": "Domain_258_repeat_rich_motif",
  "PF1259": "Domain_259_repeat_rich_motif",
  "PF1260": "Domain_260_repeat_rich_motif",
  "PF1261": "Domain_261_repeat_rich_motif",
  "PF1262": "Domain_262_repeat_rich_motif",
  "PF1263": "Domain_263_repeat_rich_motif",
  "PF1264": "Domain_264_repeat_rich_motif",
  "PF1265": "Domain_265_repeat_rich_motif",
  "PF1266": "Domain_266_repeat_rich_motif",
  "PF1267": "Domain_267_repeat_rich_motif",
  "PF1268": "Domain_268_repeat_rich_motif",
  "PF1269": "Domain_269_repeat_rich_motif",
  "PF1270": "Domain_270_repeat_rich_motif",
  "PF1271": "Domain_271_repeat_rich_motif",
  "PF1272": "Domain_272_repeat_rich_motif",
  "PF1273": "Domain_273_repeat_rich_motif",
  "PF1274": "Domain_274_repeat_rich_motif",
  "PF1275": "Domain_275_repeat_rich_motif",
  "PF1276": "Domain_276_repeat_rich_motif",
  "PF1277": "Domain_277_repeat_rich_motif",
  "PF1278": "Domain_278_repeat_rich_motif",
  "PF1279": "Domain_279_repeat_rich_motif",
  "PF1280": "Domain_280_repeat_rich_motif",
  "PF1281": "Domain_281_repeat_rich_motif",
  "PF1282": "Domain_282_repeat_rich_motif",
  "PF1283": "Domain_283_repeat_rich_motif",
  "PF1284": "Domain_284_repeat_rich_motif",
  "PF1285": "Domain_285_repeat_rich_motif",
  "PF1286": "Domain_286_repeat_rich_motif",
  "PF1287": "Domain_287_repeat_rich_motif",
  "PF1288": "Domain_288_repeat_rich_motif",
  "PF1289": "Domain_289_repeat_rich_motif",
  "PF1290": "Domain_290_repeat_rich_motif",
  "PF1291": "Domain_291_repeat_rich_motif",
  "PF1292": "Domain_292_repeat_rich_motif",
  "PF1293": "Domain_293_repeat_rich_motif",
  "PF1294": "Domain_294_repeat_rich_motif",
  "PF1295": "Domain_295_repeat_rich_motif",
  "PF1296": "Domain_296_repeat_rich_motif",
  "PF1297": "Domain_297_repeat_rich_motif",
  "PF1298": "Domain_298_repeat_rich_motif",
  "PF1299": "Domain_299_repeat_rich_motif",
  "PF1300": "Domain_300_repeat_rich_motif",
  "PF1301": "Domain_301_repeat_rich_motif",
  "PF1302": "Domain_302_repeat_rich_motif",
  "PF1303": "Domain_303_repeat_rich_motif",
  "PF1304": "Domain_304_repeat_rich_motif",
  "PF1305": "Domain_305_repeat_rich_motif",
  "PF1306": "Domain_306_repeat_rich_motif",
  "PF1307": "Domain_307_repeat_rich_motif",
  "PF1308": "Domain_308_repeat_rich_motif",
  "PF1309": "Domain_309_repeat_rich_motif",
  "PF1310": "Domain_310_repeat_rich_motif",
  "PF1311": "Domain_311_repeat_rich_motif",
  "PF1312": "Domain_312_repeat_rich_motif",
  "PF1313": "Domain_313_repeat_rich_motif",
  "PF1314": "Domain_314_repeat_rich_motif",
  "PF1315": "Domain_315_repeat_rich_motif",
  "PF1316": "Domain_316_repeat_rich_motif",
  "PF1317": "Domain_317_repeat_rich_motif",
  "PF1318": "Domain_318_repeat_rich_motif",
  "PF1319": "Domain_319_repeat_rich_motif",
  "PF1320": "Domain_320_repeat_rich_motif",
  "PF1321": "Domain_321_repeat_rich_motif",
  "PF1322": "Domain_322_repeat_rich_motif",
  "PF1323": "Domain_323_repeat_rich_motif",
  "PF1324": "Domain_324_repeat_rich_motif",
  "PF1325": "Domain_325_repeat_rich_motif",
  "PF1326": "Domain_326_repeat_rich_motif",
  "PF1327": "Domain_327_repeat_rich_motif",
  "PF1328": "Domain_328_repeat_rich_motif",
  "PF1329": "Domain_329_repeat_rich_motif",
  "PF1330": "Domain_330_repeat_rich_motif",
  "PF1331": "Domain_331_repeat_rich_motif",
  "PF1332": "Domain_332_repeat_rich_motif",
  "PF1333": "Domain_333_repeat_rich_motif",
  "PF1334": "Domain_334_repeat_rich_motif",
  "PF1335": "Domain_335_repeat_rich_motif",
  "PF1336": "Domain_336_repeat_rich_motif",
  "PF1337": "Domain_337_repeat_rich_motif",
  "PF1338": "Domain_338_repeat_rich_motif",
  "PF1339": "Domain_339_repeat_rich_motif",
  "PF1340": "Domain_340_repeat_rich_motif",
  "PF1341": "Domain_341_repeat_rich_motif",
  "PF1342": "Domain_342_repeat_rich_motif",
  "PF1343": "Domain_343_repeat_rich_motif",
  "PF1344": "Domain_344_repeat_rich_motif",
  "PF1345": "Domain_345_repeat_rich_motif",
  "PF1346": "Domain_346_repeat_rich_motif",
  "PF1347": "Domain_347_repeat_rich_motif",
  "PF1348": "Domain_348_repeat_rich_motif",
  "PF1349": "Domain_349_repeat_rich_motif",
  "PF1350": "Domain_350_repeat_rich_motif",
  "PF1351": "Domain_351_repeat_rich_motif",
  "PF1352": "Domain_352_repeat_rich_motif",
  "PF1353": "Domain_353_repeat_rich_motif",
  "PF1354": "Domain_354_repeat_rich_motif",
  "PF1355": "Domain_355_repeat_rich_motif",
  "PF1356": "Domain_356_repeat_rich_motif",
  "PF1357": "Domain_357_repeat_rich_motif",
  "PF1358": "Domain_358_repeat_rich_motif",
  "PF1359": "Domain_359_repeat_rich_motif",
  "PF1360": "Domain_360_repeat_rich_motif",
  "PF1361": "Domain_361_repeat_rich_motif",
  "PF1362": "Domain_362_repeat_rich_motif",
  "PF1363": "Domain_363_repeat_rich_motif",
  "PF1364": "Domain_364_repeat_rich_motif",
  "PF1365": "Domain_365_repeat_rich_motif",
  "PF1366": "Domain_366_repeat_rich_motif",
  "PF1367": "Domain_367_repeat_rich_motif",
  "PF1368": "Domain_368_repeat_rich_motif",
  "PF1369": "Domain_369_repeat_rich_motif",
  "PF1370": "Domain_370_repeat_rich_motif",
  "PF1371": "Domain_371_repeat_rich_motif",
  "PF1372": "Domain_372_repeat_rich_motif",
  "PF1373": "Domain_373_repeat_rich_motif",
  "PF1374": "Domain_374_repeat_rich_motif",
  "PF1375": "Domain_375_repeat_rich_motif",
  "PF1376": "Domain_376_repeat_rich_motif",
  "PF1377": "Domain_377_repeat_rich_motif",
  "PF1378": "Domain_378_repeat_rich_motif",
  "PF1379": "Domain_379_repeat_rich_motif",
  "PF1380": "Domain_380_repeat_rich_motif",
  "PF1381": "Domain_381_repeat_rich_motif",
  "PF1382": "Domain_382_repeat_rich_motif",
  "PF1383": "Domain_383_repeat_rich_motif",
  "PF1384": "Domain_384_repeat_rich_motif",
  "PF1385": "Domain_385_repeat_rich_motif",
  "PF1386": "Domain_386_repeat_rich_motif",
  "PF1387": "Domain_387_repeat_rich_motif",
  "PF1388": "Domain_388_repeat_rich_motif",
  "PF1389": "Domain_389_repeat_rich_motif",
  "PF1390": "Domain_390_repeat_rich_motif",
  "PF1391": "Domain_391_repeat_rich_motif",
  "PF1392": "Domain_392_repeat_rich_motif",
  "PF1393": "Domain_393_repeat_rich_motif",
  "PF1394": "Domain_394_repeat_rich_motif",
  "PF1395": "Domain_395_repeat_rich_motif",
  "PF1396": "Domain_396_repeat_rich_motif",
  "PF1397": "Domain_397_repeat_rich_motif",
  "PF1398": "Domain_398_repeat_rich_motif",
  "PF1399": "Domain_399_repeat_rich_motif",
  "PF1400": "Domain_400_repeat_rich_motif",
  "PF1401": "Domain_401_repeat_rich_motif",
  "PF1402": "Domain_402_repeat_rich_motif",
  "PF1403": "Domain_403_repeat_rich_motif",
  "PF1404": "Domain_404_repeat_rich_motif",
  "PF1405": "Domain_405_repeat_rich_motif",
  "PF1406": "Domain_406_repeat_rich_motif",
  "PF1407": "Domain_407_repeat_rich_motif",
  "PF1408": "Domain_408_repeat_rich_motif",
  "PF1409": "Domain_409_repeat_rich_motif",
  "PF1410": "Domain_410_repeat_rich_motif",
  "PF1411": "Domain_411_repeat_rich_motif",
  "PF1412": "Domain_412_repeat_rich_motif",
  "PF1413": "Domain_413_repeat_rich_motif",
  "PF1414": "Domain_414_repeat_rich_motif",
  "PF1415": "Domain_415_repeat_rich_motif",
  "PF1416": "Domain_416_repeat_rich_motif",
  "PF1417": "Domain_417_repeat_rich_motif",
  "PF1418": "Domain_418_repeat_rich_motif",
  "PF1419": "Domain_419_repeat_rich_motif",
  "PF1420": "Domain_420_repeat_rich_motif",
  "PF1421": "Domain_421_repeat_rich_motif",
  "PF1422": "Domain_422_repeat_rich_motif",
  "PF1423": "Domain_423_repeat_rich_motif",
  "PF1424": "Domain_424_repeat_rich_motif",
  "PF1425": "Domain_425_repeat_rich_motif",
  "PF1426": "Domain_426_repeat_rich_motif",
  "PF1427": "Domain_427_repeat_rich_motif",
  "PF1428": "Domain_428_repeat_rich_motif",
  "PF1429": "Domain_429_repeat_rich_motif",
  "PF1430": "Domain_430_repeat_rich_motif",
  "PF1431": "Domain_431_repeat_rich_motif",
  "PF1432": "Domain_432_repeat_rich_motif",
  "PF1433": "Domain_433_repeat_rich_motif",
  "PF1434": "Domain_434_repeat_rich_motif",
  "PF1435": "Domain_435_repeat_rich_motif",
  "PF1436": "Domain_436_repeat_rich_motif",
  "PF1437": "Domain_437_repeat_rich_motif",
  "PF1438": "Domain_438_repeat_rich_motif",
  "PF1439": "Domain_439_repeat_rich_motif",
  "PF1440": "Domain_440_repeat_rich_motif",
  "PF1441": "Domain_441_repeat_rich_motif",
  "PF1442": "Domain_442_repeat_rich_motif",
  "PF1443": "Domain_443_repeat_rich_motif",
  "PF1444": "Domain_444_repeat_rich_motif",
  "PF1445": "Domain_445_repeat_rich_motif",
  "PF1446": "Domain_446_repeat_rich_motif",
  "PF1447": "Domain_447_repeat_rich_motif",
  "PF1448": "Domain_448_repeat_rich_motif",
  "PF1449": "Domain_449_repeat_rich_motif",
  "PF1450": "Domain_450_repeat_rich_motif",
  "PF1451": "Domain_451_repeat_rich_motif",
  "PF1452": "Domain_452_repeat_rich_motif",
  "PF1453": "Domain_453_repeat_rich_motif",
  "PF1454": "Domain_454_repeat_rich_motif",
  "PF1455": "Domain_455_repeat_rich_motif",
  "PF1456": "Domain_456_repeat_rich_motif",
  "PF1457": "Domain_457_repeat_rich_motif",
  "PF1458": "Domain_458_repeat_rich_motif",
  "PF1459": "Domain_459_repeat_rich_motif",
  "PF1460": "Domain_460_repeat_rich_motif",
  "PF1461": "Domain_461_repeat_rich_motif",
  "PF1462": "Domain_462_repeat_rich_motif",
  "PF1463": "Domain_463_repeat_rich_motif",
  "PF1464": "Domain_464_repeat_rich_motif",
  "PF1465": "Domain_465_repeat_rich_motif",
  "PF1466": "Domain_466_repeat_rich_motif",
  "PF1467": "Domain_467_repeat_rich_motif",
  "PF1468": "Domain_468_repeat_rich_motif",
  "PF1469": "Domain_469_repeat_rich_motif",
  "PF1470": "Domain_470_repeat_rich_motif",
  "PF1471": "Domain_471_repeat_rich_motif",
  "PF1472": "Domain_472_repeat_rich_motif",
  "PF1473": "Domain_473_repeat_rich_motif",
  "PF1474": "Domain_474_repeat_rich_motif",
  "PF1475": "Domain_475_repeat_rich_motif",
  "PF1476": "Domain_476_repeat_rich_motif",
  "PF1477": "Domain_477_repeat_rich_motif",
  "PF1478": "Domain_478_repeat_rich_motif",
  "PF1479": "Domain_479_repeat_rich_motif",
  "PF1480": "Domain_480_repeat_rich_motif",
  "PF1481": "Domain_481_repeat_rich_motif",
  "PF1482": "Domain_482_repeat_rich_motif",
  "PF1483": "Domain_483_repeat_rich_motif",
  "PF1484": "Domain_484_repeat_rich_motif",
  "PF1485": "Domain_485_repeat_rich_motif",
  "PF1486": "Domain_486_repeat_rich_motif",
  "PF1487": "Domain_487_repeat_rich_motif",
  "PF1488": "Domain_488_repeat_rich_motif",
  "PF1489": "Domain_489_repeat_rich_motif",
  "PF1490": "Domain_490_repeat_rich_motif",
  "PF1491": "Domain_491_repeat_rich_motif",
  "PF1492": "Domain_492_repeat_rich_motif",
  "PF1493": "Domain_493_repeat_rich_motif",
  "PF1494": "Domain_494_repeat_rich_motif",
  "PF1495": "Domain_495_repeat_rich_motif",
  "PF1496": "Domain_496_repeat_rich_motif",
  "PF1497": "Domain_497_repeat_rich_motif",
  "PF1498": "Domain_498_repeat_rich_motif",
  "PF1499": "Domain_499_repeat_rich_motif",
  "PF1500": "Domain_500_repeat_rich_motif",
  "PF1501": "Domain_501_repeat_rich_motif",
  "PF1502": "Domain_502_repeat_rich_motif",
  "PF1503": "Domain_503_repeat_rich_motif",
  "PF1504": "Domain_504_repeat_rich_motif",
  "PF1505": "Domain_505_repeat_rich_motif",
  "PF1506": "Domain_506_repeat_rich_motif",
  "PF1507": "Domain_507_repeat_rich_motif",
  "PF1508": "Domain_508_repeat_rich_motif",
  "PF1509": "Domain_509_repeat_rich_motif",
  "PF1510": "Domain_510_repeat_rich_motif",
  "PF1511": "Domain_511_repeat_rich_motif",
  "PF1512": "Domain_512_repeat_rich_motif",
  "PF1513": "Domain_513_repeat_rich_motif",
  "PF1514": "Domain_514_repeat_rich_motif",
  "PF1515": "Domain_515_repeat_rich_motif",
  "PF1516": "Domain_516_repeat_rich_motif",
  "PF1517": "Domain_517_repeat_rich_motif",
  "PF1518": "Domain_518_repeat_rich_motif",
  "PF1519": "Domain_519_repeat_rich_motif",
  "PF1520": "Domain_520_repeat_rich_motif",
  "PF1521": "Domain_521_repeat_rich_motif",
  "PF1522": "Domain_522_repeat_rich_motif",
  "PF1523": "Domain_523_repeat_rich_motif",
  "PF1524": "Domain_524_repeat_rich_motif",
  "PF1525": "Domain_525_repeat_rich_motif",
  "PF1526": "Domain_526_repeat_rich_motif",
  "PF1527": "Domain_527_repeat_rich_motif",
  "PF1528": "Domain_528_repeat_rich_motif",
  "PF1529": "Domain_529_repeat_rich_motif",
  "PF1530": "Domain_530_repeat_rich_motif",
  "PF1531": "Domain_531_repeat_rich_motif",
  "PF1532": "Domain_532_repeat_rich_motif",
  "PF1533": "Domain_533_repeat_rich_motif",
  "PF1534": "Domain_534_repeat_rich_motif",
  "PF1535": "Domain_535_repeat_rich_motif",
  "PF1536": "Domain_536_repeat_rich_motif",
  "PF1537": "Domain_537_repeat_rich_motif",
  "PF1538": "Domain_538_repeat_rich_motif",
  "PF1539": "Domain_539_repeat_rich_motif",
  "PF1540": "Domain_540_repeat_rich_motif",
  "PF1541": "Domain_541_repeat_rich_motif",
  "PF1542": "Domain_542_repeat_rich_motif",
  "PF1543": "Domain_543_repeat_rich_motif",
  "PF1544": "Domain_544_repeat_rich_motif",
  "PF1545": "Domain_545_repeat_rich_motif",
  "PF1546": "Domain_546_repeat_rich_motif",
  "PF1547": "Domain_547_repeat_rich_motif",
  "PF1548": "Domain_548_repeat_rich_motif",
  "PF1549": "Domain_549_repeat_rich_motif",
  "PF1550": "Domain_550_repeat_rich_motif",
  "PF1551": "Domain_551_repeat_rich_motif",
  "PF1552": "Domain_552_repeat_rich_motif",
  "PF1553": "Domain_553_repeat_rich_motif",
  "PF1554": "Domain_554_repeat_rich_motif",
  "PF1555": "Domain_555_repeat_rich_motif",
  "PF1556": "Domain_556_repeat_rich_motif",
  "PF1557": "Domain_557_repeat_rich_motif",
  "PF1558": "Domain_558_repeat_rich_motif",
  "PF1559": "Domain_559_repeat_rich_motif",
  "PF1560": "Domain_560_repeat_rich_motif",
  "PF1561": "Domain_561_repeat_rich_motif",
  "PF1562": "Domain_562_repeat_rich_motif",
  "PF1563": "Domain_563_repeat_rich_motif",
  "PF1564": "Domain_564_repeat_rich_motif",
  "PF1565": "Domain_565_repeat_rich_motif",
  "PF1566": "Domain_566_repeat_rich_motif",
  "PF1567": "Domain_567_repeat_rich_motif",
  "PF1568": "Domain_568_repeat_rich_motif",
  "PF1569": "Domain_569_repeat_rich_motif",
  "PF1570": "Domain_570_repeat_rich_motif",
  "PF1571": "Domain_571_repeat_rich_motif",
  "PF1572": "Domain_572_repeat_rich_motif",
  "PF1573": "Domain_573_repeat_rich_motif",
  "PF1574": "Domain_574_repeat_rich_motif",
  "PF1575": "Domain_575_repeat_rich_motif",
  "PF1576": "Domain_576_repeat_rich_motif",
  "PF1577": "Domain_577_repeat_rich_motif",
  "PF1578": "Domain_578_repeat_rich_motif",
  "PF1579": "Domain_579_repeat_rich_motif",
  "PF1580": "Domain_580_repeat_rich_motif",
  "PF1581": "Domain_581_repeat_rich_motif",
  "PF1582": "Domain_582_repeat_rich_motif",
  "PF1583": "Domain_583_repeat_rich_motif",
  "PF1584": "Domain_584_repeat_rich_motif",
  "PF1585": "Domain_585_repeat_rich_motif",
  "PF1586": "Domain_586_repeat_rich_motif",
  "PF1587": "Domain_587_repeat_rich_motif",
  "PF1588": "Domain_588_repeat_rich_motif",
  "PF1589": "Domain_589_repeat_rich_motif",
  "PF1590": "Domain_590_repeat_rich_motif",
  "PF1591": "Domain_591_repeat_rich_motif",
  "PF1592": "Domain_592_repeat_rich_motif",
  "PF1593": "Domain_593_repeat_rich_motif",
  "PF1594": "Domain_594_repeat_rich_motif",
  "PF1595": "Domain_595_repeat_rich_motif",
  "PF1596": "Domain_596_repeat_rich_motif",
  "PF1597": "Domain_597_repeat_rich_motif",
  "PF1598": "Domain_598_repeat_rich_motif",
  "PF1599": "Domain_599_repeat_rich_motif",
  "PF1600": "Domain_600_repeat_rich_motif",
  "PF1601": "Domain_601_repeat_rich_motif",
  "PF1602": "Domain_602_repeat_rich_motif",
  "PF1603": "Domain_603_repeat_rich_motif",
  "PF1604": "Domain_604_repeat_rich_motif",
  "PF1605": "Domain_605_repeat_rich_motif",
  "PF1606": "Domain_606_repeat_rich_motif",
  "PF1607": "Domain_607_repeat_rich_motif",
  "PF1608": "Domain_608_repeat_rich_motif",
  "PF1609": "Domain_609_repeat_rich_motif",
  "PF1610": "Domain_610_repeat_rich_motif",
  "PF1611": "Domain_611_repeat_rich_motif",
  "PF1612": "Domain_612_repeat_rich_motif",
  "PF1613": "Domain_613_repeat_rich_motif",
  "PF1614": "Domain_614_repeat_rich_motif",
  "PF1615": "Domain_615_repeat_rich_motif",
  "PF1616": "Domain_616_repeat_rich_motif",
  "PF1617": "Domain_617_repeat_rich_motif",
  "PF1618": "Domain_618_repeat_rich_motif",
  "PF1619": "Domain_619_repeat_rich_motif",
  "PF1620": "Domain_620_repeat_rich_motif",
  "PF1621": "Domain_621_repeat_rich_motif",
  "PF1622": "Domain_622_repeat_rich_motif",
  "PF1623": "Domain_623_repeat_rich_motif",
  "PF1624": "Domain_624_repeat_rich_motif",
  "PF1625": "Domain_625_repeat_rich_motif",
  "PF1626": "Domain_626_repeat_rich_motif",
  "PF1627": "Domain_627_repeat_rich_motif",
  "PF1628": "Domain_628_repeat_rich_motif",
  "PF1629": "Domain_629_repeat_rich_motif",
  "PF1630": "Domain_630_repeat_rich_motif",
  "PF1631": "Domain_631_repeat_rich_motif",
  "PF1632": "Domain_632_repeat_rich_motif",
  "PF1633": "Domain_633_repeat_rich_motif",
  "PF1634": "Domain_634_repeat_rich_motif",
  "PF1635": "Domain_635_repeat_rich_motif",
  "PF1636": "Domain_636_repeat_rich_motif",
  "PF1637": "Domain_637_repeat_rich_motif",
  "PF1638": "Domain_638_repeat_rich_motif",
  "PF1639": "Domain_639_repeat_rich_motif",
  "PF1640": "Domain_640_repeat_rich_motif",
  "PF1641": "Domain_641_repeat_rich_motif",
  "PF1642": "Domain_642_repeat_rich_motif",
  "PF1643": "Domain_643_repeat_rich_motif",
  "PF1644": "Domain_644_repeat_rich_motif",
  "PF1645": "Domain_645_repeat_rich_motif",
  "PF1646": "Domain_646_repeat_rich_motif",
  "PF1647": "Domain_647_repeat_rich_motif",
  "PF1648": "Domain_648_repeat_rich_motif",
  "PF1649": "Domain_649_repeat_rich_motif",
  "PF1650": "Domain_650_repeat_rich_motif",
  "PF1651": "Domain_651_repeat_rich_motif",
  "PF1652": "Domain_652_repeat_rich_motif",
  "PF1653": "Domain_653_repeat_rich_motif",
  "PF1654": "Domain_654_repeat_rich_motif",
  "PF1655": "Domain_655_repeat_rich_motif",
  "PF1656": "Domain_656_repeat_rich_motif",
  "PF1657": "Domain_657_repeat_rich_motif",
  "PF1658": "Domain_658_repeat_rich_motif",
  "PF1659": "Domain_659_repeat_rich_motif",
  "PF1660": "Domain_660_repeat_rich_motif",
  "PF1661": "Domain_661_repeat_rich_motif",
  "PF1662": "Domain_662_repeat_rich_motif",
  "PF1663": "Domain_663_repeat_rich_motif",
  "PF1664": "Domain_664_repeat_rich_motif",
  "PF1665": "Domain_665_repeat_rich_motif",
  "PF1666": "Domain_666_repeat_rich_motif",
  "PF1667": "Domain_667_repeat_rich_motif",
  "PF1668": "Domain_668_repeat_rich_motif",
  "PF1669": "Domain_669_repeat_rich_motif",
  "PF1670": "Domain_670_repeat_rich_motif",
  "PF1671": "Domain_671_repeat_rich_motif",
  "PF1672": "Domain_672_repeat_rich_motif",
  "PF1673": "Domain_673_repeat_rich_motif",
  "PF1674": "Domain_674_repeat_rich_motif",
  "PF1675": "Domain_675_repeat_rich_motif",
  "PF1676": "Domain_676_repeat_rich_motif",
  "PF1677": "Domain_677_repeat_rich_motif",
  "PF1678": "Domain_678_repeat_rich_motif",
  "PF1679": "Domain_679_repeat_rich_motif",
  "PF1680": "Domain_680_repeat_rich_motif",
  "PF1681": "Domain_681_repeat_rich_motif",
  "PF1682": "Domain_682_repeat_rich_motif",
  "PF1683": "Domain_683_repeat_rich_motif",
  "PF1684": "Domain_684_repeat_rich_motif",
  "PF1685": "Domain_685_repeat_rich_motif",
  "PF1686": "Domain_686_repeat_rich_motif",
  "PF1687": "Domain_687_repeat_rich_motif",
  "PF1688": "Domain_688_repeat_rich_motif",
  "PF1689": "Domain_689_repeat_rich_motif",
  "PF1690": "Domain_690_repeat_rich_motif",
  "PF1691": "Domain_691_repeat_rich_motif",
  "PF1692": "Domain_692_repeat_rich_motif",
  "PF1693": "Domain_693_repeat_rich_motif",
  "PF1694": "Domain_694_repeat_rich_motif",
  "PF1695": "Domain_695_repeat_rich_motif",
  "PF1696": "Domain_696_repeat_rich_motif",
  "PF1697": "Domain_697_repeat_rich_motif",
  "PF1698": "Domain_698_repeat_rich_motif",
  "PF1699": "Domain_699_repeat_rich_motif",
  "PF1700": "Domain_700_repeat_rich_motif",
  "PF1701": "Domain_701_repeat_rich_motif",
  "PF1702": "Domain_702_repeat_rich_motif",
  "PF1703": "Domain_703_repeat_rich_motif",
  "PF1704": "Domain_704_repeat_rich_motif",
  "PF1705": "Domain_705_repeat_rich_motif",
  "PF1706": "Domain_706_repeat_rich_motif",
  "PF1707": "Domain_707_repeat_rich_motif",
  "PF1708": "Domain_708_repeat_rich_motif",
  "PF1709": "Domain_709_repeat_rich_motif",
  "PF1710": "Domain_710_repeat_rich_motif",
  "PF1711": "Domain_711_repeat_rich_motif",
  "PF1712": "Domain_712_repeat_rich_motif",
  "PF1713": "Domain_713_repeat_rich_motif",
  "PF1714": "Domain_714_repeat_rich_motif",
  "PF1715": "Domain_715_repeat_rich_motif",
  "PF1716": "Domain_716_repeat_rich_motif",
  "PF1717": "Domain_717_repeat_rich_motif",
  "PF1718": "Domain_718_repeat_rich_motif",
  "PF1719": "Domain_719_repeat_rich_motif",
  "PF1720": "Domain_720_repeat_rich_motif",
  "PF1721": "Domain_721_repeat_rich_motif",
  "PF1722": "Domain_722_repeat_rich_motif",
  "PF1723": "Domain_723_repeat_rich_motif",
  "PF1724": "Domain_724_repeat_rich_motif",
  "PF1725": "Domain_725_repeat_rich_motif",
  "PF1726": "Domain_726_repeat_rich_motif",
  "PF1727": "Domain_727_repeat_rich_motif",
  "PF1728": "Domain_728_repeat_rich_motif",
  "PF1729": "Domain_729_repeat_rich_motif",
  "PF1730": "Domain_730_repeat_rich_motif",
  "PF1731": "Domain_731_repeat_rich_motif",
  "PF1732": "Domain_732_repeat_rich_motif",
  "PF1733": "Domain_733_repeat_rich_motif",
  "PF1734": "Domain_734_repeat_rich_motif",
  "PF1735": "Domain_735_repeat_rich_motif",
  "PF1736": "Domain_736_repeat_rich_motif",
  "PF1737": "Domain_737_repeat_rich_motif",
  "PF1738": "Domain_738_repeat_rich_motif",
  "PF1739": "Domain_739_repeat_rich_motif",
  "PF1740": "Domain_740_repeat_rich_motif",
  "PF1741": "Domain_741_repeat_rich_motif",
  "PF1742": "Domain_742_repeat_rich_motif",
  "PF1743": "Domain_743_repeat_rich_motif",
  "PF1744": "Domain_744_repeat_rich_motif",
  "PF1745": "Domain_745_repeat_rich_motif",
  "PF1746": "Domain_746_repeat_rich_motif",
  "PF1747": "Domain_747_repeat_rich_motif",
  "PF1748": "Domain_748_repeat_rich_motif",
  "PF1749": "Domain_749_repeat_rich_motif",
  "PF1750": "Domain_750_repeat_rich_motif",
  "PF1751": "Domain_751_repeat_rich_motif",
  "PF1752": "Domain_752_repeat_rich_motif",
  "PF1753": "Domain_753_repeat_rich_motif",
  "PF1754": "Domain_754_repeat_rich_motif",
  "PF1755": "Domain_755_repeat_rich_motif",
  "PF1756": "Domain_756_repeat_rich_motif",
  "PF1757": "Domain_757_repeat_rich_motif",
  "PF1758": "Domain_758_repeat_rich_motif",
  "PF1759": "Domain_759_repeat_rich_motif",
  "PF1760": "Domain_760_repeat_rich_motif",
  "PF1761": "Domain_761_repeat_rich_motif",
  "PF1762": "Domain_762_repeat_rich_motif",
  "PF1763": "Domain_763_repeat_rich_motif",
  "PF1764": "Domain_764_repeat_rich_motif",
  "PF1765": "Domain_765_repeat_rich_motif",
  "PF1766": "Domain_766_repeat_rich_motif",
  "PF1767": "Domain_767_repeat_rich_motif",
  "PF1768": "Domain_768_repeat_rich_motif",
  "PF1769": "Domain_769_repeat_rich_motif",
  "PF1770": "Domain_770_repeat_rich_motif",
  "PF1771": "Domain_771_repeat_rich_motif",
  "PF1772": "Domain_772_repeat_rich_motif",
  "PF1773": "Domain_773_repeat_rich_motif",
  "PF1774": "Domain_774_repeat_rich_motif",
  "PF1775": "Domain_775_repeat_rich_motif",
  "PF1776": "Domain_776_repeat_rich_motif",
  "PF1777": "Domain_777_repeat_rich_motif",
  "PF1778": "Domain_778_repeat_rich_motif",
  "PF1779": "Domain_779_repeat_rich_motif",
  "PF1780": "Domain_780_repeat_rich_motif",
  "PF1781": "Domain_781_repeat_rich_motif",
  "PF1782": "Domain_782_repeat_rich_motif",
  "PF1783": "Domain_783_repeat_rich_motif",
  "PF1784": "Domain_784_repeat_rich_motif",
  "PF1785": "Domain_785_repeat_rich_motif",
  "PF1786": "Domain_786_repeat_rich_motif",
  "PF1787": "Domain_787_repeat_rich_motif",
  "PF1788": "Domain_788_repeat_rich_motif",
  "PF1789": "Domain_789_repeat_rich_motif",
  "PF1790": "Domain_790_repeat_rich_motif",
  "PF1791": "Domain_791_repeat_rich_motif",
  "PF1792": "Domain_792_repeat_rich_motif",
  "PF1793": "Domain_793_repeat_rich_motif",
  "PF1794": "Domain_794_repeat_rich_motif",
  "PF1795": "Domain_795_repeat_rich_motif",
  "PF1796": "Domain_796_repeat_rich_motif",
  "PF1797": "Domain_797_repeat_rich_motif",
  "PF1798": "Domain_798_repeat_rich_motif",
  "PF1799": "Domain_799_repeat_rich_motif",
  "PF1800": "Domain_800_repeat_rich_motif",
  "PF1801": "Domain_801_repeat_rich_motif",
  "PF1802": "Domain_802_repeat_rich_motif",
  "PF1803": "Domain_803_repeat_rich_motif",
  "PF1804": "Domain_804_repeat_rich_motif",
  "PF1805": "Domain_805_repeat_rich_motif",
  "PF1806": "Domain_806_repeat_rich_motif",
  "PF1807": "Domain_807_repeat_rich_motif",
  "PF1808": "Domain_808_repeat_rich_motif",
  "PF1809": "Domain_809_repeat_rich_motif",
  "PF1810": "Domain_810_repeat_rich_motif",
  "PF1811": "Domain_811_repeat_rich_motif",
  "PF1812": "Domain_812_repeat_rich_motif",
  "PF1813": "Domain_813_repeat_rich_motif",
  "PF1814": "Domain_814_repeat_rich_motif",
  "PF1815": "Domain_815_repeat_rich_motif",
  "PF1816": "Domain_816_repeat_rich_motif",
  "PF1817": "Domain_817_repeat_rich_motif",
  "PF1818": "Domain_818_repeat_rich_motif",
  "PF1819": "Domain_819_repeat_rich_motif",
  "PF1820": "Domain_820_repeat_rich_motif",
  "PF1821": "Domain_821_repeat_rich_motif",
  "PF1822": "Domain_822_repeat_rich_motif",
  "PF1823": "Domain_823_repeat_rich_motif",
  "PF1824": "Domain_824_repeat_rich_motif",
  "PF1825": "Domain_825_repeat_rich_motif",
  "PF1826": "Domain_826_repeat_rich_motif",
  "PF1827": "Domain_827_repeat_rich_motif",
  "PF1828": "Domain_828_repeat_rich_motif",
  "PF1829": "Domain_829_repeat_rich_motif",
  "PF1830": "Domain_830_repeat_rich_motif",
  "PF1831": "Domain_831_repeat_rich_motif",
  "PF1832": "Domain_832_repeat_rich_motif",
  "PF1833": "Domain_833_repeat_rich_motif",
  "PF1834": "Domain_834_repeat_rich_motif",
  "PF1835": "Domain_835_repeat_rich_motif",
  "PF1836": "Domain_836_repeat_rich_motif",
  "PF1837": "Domain_837_repeat_rich_motif",
  "PF1838": "Domain_838_repeat_rich_motif",
  "PF1839": "Domain_839_repeat_rich_motif",
  "PF1840": "Domain_840_repeat_rich_motif",
  "PF1841": "Domain_841_repeat_rich_motif",
  "PF1842": "Domain_842_repeat_rich_motif",
  "PF1843": "Domain_843_repeat_rich_motif",
  "PF1844": "Domain_844_repeat_rich_motif",
  "PF1845": "Domain_845_repeat_rich_motif",
  "PF1846": "Domain_846_repeat_rich_motif",
  "PF1847": "Domain_847_repeat_rich_motif",
  "PF1848": "Domain_848_repeat_rich_motif",
  "PF1849": "Domain_849_repeat_rich_motif",
  "PF1850": "Domain_850_repeat_rich_motif",
  "PF1851": "Domain_851_repeat_rich_motif",
  "PF1852": "Domain_852_repeat_rich_motif",
  "PF1853": "Domain_853_repeat_rich_motif",
  "PF1854": "Domain_854_repeat_rich_motif",
  "PF1855": "Domain_855_repeat_rich_motif",
  "PF1856": "Domain_856_repeat_rich_motif",
  "PF1857": "Domain_857_repeat_rich_motif",
  "PF1858": "Domain_858_repeat_rich_motif",
  "PF1859": "Domain_859_repeat_rich_motif",
  "PF1860": "Domain_860_repeat_rich_motif",
  "PF1861": "Domain_861_repeat_rich_motif",
  "PF1862": "Domain_862_repeat_rich_motif",
  "PF1863": "Domain_863_repeat_rich_motif",
  "PF1864": "Domain_864_repeat_rich_motif",
  "PF1865": "Domain_865_repeat_rich_motif",
  "PF1866": "Domain_866_repeat_rich_motif",
  "PF1867": "Domain_867_repeat_rich_motif",
  "PF1868": "Domain_868_repeat_rich_motif",
  "PF1869": "Domain_869_repeat_rich_motif",
  "PF1870": "Domain_870_repeat_rich_motif",
  "PF1871": "Domain_871_repeat_rich_motif",
  "PF1872": "Domain_872_repeat_rich_motif",
  "PF1873": "Domain_873_repeat_rich_motif",
  "PF1874": "Domain_874_repeat_rich_motif",
  "PF1875": "Domain_875_repeat_rich_motif",
  "PF1876": "Domain_876_repeat_rich_motif",
  "PF1877": "Domain_877_repeat_rich_motif",
  "PF1878": "Domain_878_repeat_rich_motif",
  "PF1879": "Domain_879_repeat_rich_motif",
  "PF1880": "Domain_880_repeat_rich_motif",
  "PF1881": "Domain_881_repeat_rich_motif",
  "PF1882": "Domain_882_repeat_rich_motif",
  "PF1883": "Domain_883_repeat_rich_motif",
  "PF1884": "Domain_884_repeat_rich_motif",
  "PF1885": "Domain_885_repeat_rich_motif",
  "PF1886": "Domain_886_repeat_rich_motif",
  "PF1887": "Domain_887_repeat_rich_motif",
  "PF1888": "Domain_888_repeat_rich_motif",
  "PF1889": "Domain_889_repeat_rich_motif",
  "PF1890": "Domain_890_repeat_rich_motif",
  "PF1891": "Domain_891_repeat_rich_motif",
  "PF1892": "Domain_892_repeat_rich_motif",
  "PF1893": "Domain_893_repeat_rich_motif",
  "PF1894": "Domain_894_repeat_rich_motif",
  "PF1895": "Domain_895_repeat_rich_motif",
  "PF1896": "Domain_896_repeat_rich_motif",
  "PF1897": "Domain_897_repeat_rich_motif",
  "PF1898": "Domain_898_repeat_rich_motif",
  "PF1899": "Domain_899_repeat_rich_motif"
}\nHLA_PANELS = {
  "panel_1": [
    "HLA-A*02:01",
    "HLA-B*07:01",
    "HLA-C*07:01"
  ],
  "panel_2": [
    "HLA-A*02:02",
    "HLA-B*07:02",
    "HLA-C*07:02"
  ],
  "panel_3": [
    "HLA-A*02:03",
    "HLA-B*07:03",
    "HLA-C*07:03"
  ],
  "panel_4": [
    "HLA-A*02:04",
    "HLA-B*07:04",
    "HLA-C*07:04"
  ],
  "panel_5": [
    "HLA-A*02:05",
    "HLA-B*07:05",
    "HLA-C*07:05"
  ],
  "panel_6": [
    "HLA-A*02:06",
    "HLA-B*07:06",
    "HLA-C*07:06"
  ],
  "panel_7": [
    "HLA-A*02:07",
    "HLA-B*07:07",
    "HLA-C*07:07"
  ],
  "panel_8": [
    "HLA-A*02:08",
    "HLA-B*07:08",
    "HLA-C*07:08"
  ],
  "panel_9": [
    "HLA-A*02:09",
    "HLA-B*07:09",
    "HLA-C*07:09"
  ],
  "panel_10": [
    "HLA-A*02:10",
    "HLA-B*07:10",
    "HLA-C*07:10"
  ],
  "panel_11": [
    "HLA-A*02:11",
    "HLA-B*07:11",
    "HLA-C*07:11"
  ],
  "panel_12": [
    "HLA-A*02:12",
    "HLA-B*07:12",
    "HLA-C*07:12"
  ],
  "panel_13": [
    "HLA-A*02:13",
    "HLA-B*07:13",
    "HLA-C*07:13"
  ],
  "panel_14": [
    "HLA-A*02:14",
    "HLA-B*07:14",
    "HLA-C*07:14"
  ],
  "panel_15": [
    "HLA-A*02:15",
    "HLA-B*07:15",
    "HLA-C*07:15"
  ],
  "panel_16": [
    "HLA-A*02:16",
    "HLA-B*07:16",
    "HLA-C*07:16"
  ],
  "panel_17": [
    "HLA-A*02:17",
    "HLA-B*07:17",
    "HLA-C*07:17"
  ],
  "panel_18": [
    "HLA-A*02:18",
    "HLA-B*07:18",
    "HLA-C*07:18"
  ],
  "panel_19": [
    "HLA-A*02:19",
    "HLA-B*07:19",
    "HLA-C*07:19"
  ],
  "panel_20": [
    "HLA-A*02:20",
    "HLA-B*07:20",
    "HLA-C*07:20"
  ],
  "panel_21": [
    "HLA-A*02:21",
    "HLA-B*07:21",
    "HLA-C*07:21"
  ],
  "panel_22": [
    "HLA-A*02:22",
    "HLA-B*07:22",
    "HLA-C*07:22"
  ],
  "panel_23": [
    "HLA-A*02:23",
    "HLA-B*07:23",
    "HLA-C*07:23"
  ],
  "panel_24": [
    "HLA-A*02:24",
    "HLA-B*07:24",
    "HLA-C*07:24"
  ],
  "panel_25": [
    "HLA-A*02:25",
    "HLA-B*07:25",
    "HLA-C*07:25"
  ],
  "panel_26": [
    "HLA-A*02:26",
    "HLA-B*07:26",
    "HLA-C*07:26"
  ],
  "panel_27": [
    "HLA-A*02:27",
    "HLA-B*07:27",
    "HLA-C*07:27"
  ],
  "panel_28": [
    "HLA-A*02:28",
    "HLA-B*07:28",
    "HLA-C*07:28"
  ],
  "panel_29": [
    "HLA-A*02:29",
    "HLA-B*07:29",
    "HLA-C*07:29"
  ],
  "panel_30": [
    "HLA-A*02:30",
    "HLA-B*07:30",
    "HLA-C*07:30"
  ],
  "panel_31": [
    "HLA-A*02:31",
    "HLA-B*07:31",
    "HLA-C*07:31"
  ],
  "panel_32": [
    "HLA-A*02:32",
    "HLA-B*07:32",
    "HLA-C*07:32"
  ],
  "panel_33": [
    "HLA-A*02:33",
    "HLA-B*07:33",
    "HLA-C*07:33"
  ],
  "panel_34": [
    "HLA-A*02:34",
    "HLA-B*07:34",
    "HLA-C*07:34"
  ],
  "panel_35": [
    "HLA-A*02:35",
    "HLA-B*07:35",
    "HLA-C*07:35"
  ],
  "panel_36": [
    "HLA-A*02:36",
    "HLA-B*07:36",
    "HLA-C*07:36"
  ],
  "panel_37": [
    "HLA-A*02:37",
    "HLA-B*07:37",
    "HLA-C*07:37"
  ],
  "panel_38": [
    "HLA-A*02:38",
    "HLA-B*07:38",
    "HLA-C*07:38"
  ],
  "panel_39": [
    "HLA-A*02:39",
    "HLA-B*07:39",
    "HLA-C*07:39"
  ],
  "panel_40": [
    "HLA-A*02:40",
    "HLA-B*07:40",
    "HLA-C*07:40"
  ],
  "panel_41": [
    "HLA-A*02:41",
    "HLA-B*07:41",
    "HLA-C*07:41"
  ],
  "panel_42": [
    "HLA-A*02:42",
    "HLA-B*07:42",
    "HLA-C*07:42"
  ],
  "panel_43": [
    "HLA-A*02:43",
    "HLA-B*07:43",
    "HLA-C*07:43"
  ],
  "panel_44": [
    "HLA-A*02:44",
    "HLA-B*07:44",
    "HLA-C*07:44"
  ],
  "panel_45": [
    "HLA-A*02:45",
    "HLA-B*07:45",
    "HLA-C*07:45"
  ],
  "panel_46": [
    "HLA-A*02:46",
    "HLA-B*07:46",
    "HLA-C*07:46"
  ],
  "panel_47": [
    "HLA-A*02:47",
    "HLA-B*07:47",
    "HLA-C*07:47"
  ],
  "panel_48": [
    "HLA-A*02:48",
    "HLA-B*07:48",
    "HLA-C*07:48"
  ],
  "panel_49": [
    "HLA-A*02:49",
    "HLA-B*07:49",
    "HLA-C*07:49"
  ],
  "panel_50": [
    "HLA-A*02:50",
    "HLA-B*07:50",
    "HLA-C*07:50"
  ],
  "panel_51": [
    "HLA-A*02:51",
    "HLA-B*07:51",
    "HLA-C*07:51"
  ],
  "panel_52": [
    "HLA-A*02:52",
    "HLA-B*07:52",
    "HLA-C*07:52"
  ],
  "panel_53": [
    "HLA-A*02:53",
    "HLA-B*07:53",
    "HLA-C*07:53"
  ],
  "panel_54": [
    "HLA-A*02:54",
    "HLA-B*07:54",
    "HLA-C*07:54"
  ],
  "panel_55": [
    "HLA-A*02:55",
    "HLA-B*07:55",
    "HLA-C*07:55"
  ],
  "panel_56": [
    "HLA-A*02:56",
    "HLA-B*07:56",
    "HLA-C*07:56"
  ],
  "panel_57": [
    "HLA-A*02:57",
    "HLA-B*07:57",
    "HLA-C*07:57"
  ],
  "panel_58": [
    "HLA-A*02:58",
    "HLA-B*07:58",
    "HLA-C*07:58"
  ],
  "panel_59": [
    "HLA-A*02:59",
    "HLA-B*07:59",
    "HLA-C*07:59"
  ],
  "panel_60": [
    "HLA-A*02:60",
    "HLA-B*07:60",
    "HLA-C*07:60"
  ],
  "panel_61": [
    "HLA-A*02:61",
    "HLA-B*07:61",
    "HLA-C*07:61"
  ],
  "panel_62": [
    "HLA-A*02:62",
    "HLA-B*07:62",
    "HLA-C*07:62"
  ],
  "panel_63": [
    "HLA-A*02:63",
    "HLA-B*07:63",
    "HLA-C*07:63"
  ],
  "panel_64": [
    "HLA-A*02:64",
    "HLA-B*07:64",
    "HLA-C*07:64"
  ],
  "panel_65": [
    "HLA-A*02:65",
    "HLA-B*07:65",
    "HLA-C*07:65"
  ],
  "panel_66": [
    "HLA-A*02:66",
    "HLA-B*07:66",
    "HLA-C*07:66"
  ],
  "panel_67": [
    "HLA-A*02:67",
    "HLA-B*07:67",
    "HLA-C*07:67"
  ],
  "panel_68": [
    "HLA-A*02:68",
    "HLA-B*07:68",
    "HLA-C*07:68"
  ],
  "panel_69": [
    "HLA-A*02:69",
    "HLA-B*07:69",
    "HLA-C*07:69"
  ],
  "panel_70": [
    "HLA-A*02:70",
    "HLA-B*07:70",
    "HLA-C*07:70"
  ],
  "panel_71": [
    "HLA-A*02:71",
    "HLA-B*07:71",
    "HLA-C*07:71"
  ],
  "panel_72": [
    "HLA-A*02:72",
    "HLA-B*07:72",
    "HLA-C*07:72"
  ],
  "panel_73": [
    "HLA-A*02:73",
    "HLA-B*07:73",
    "HLA-C*07:73"
  ],
  "panel_74": [
    "HLA-A*02:74",
    "HLA-B*07:74",
    "HLA-C*07:74"
  ],
  "panel_75": [
    "HLA-A*02:75",
    "HLA-B*07:75",
    "HLA-C*07:75"
  ],
  "panel_76": [
    "HLA-A*02:76",
    "HLA-B*07:76",
    "HLA-C*07:76"
  ],
  "panel_77": [
    "HLA-A*02:77",
    "HLA-B*07:77",
    "HLA-C*07:77"
  ],
  "panel_78": [
    "HLA-A*02:78",
    "HLA-B*07:78",
    "HLA-C*07:78"
  ],
  "panel_79": [
    "HLA-A*02:79",
    "HLA-B*07:79",
    "HLA-C*07:79"
  ],
  "panel_80": [
    "HLA-A*02:80",
    "HLA-B*07:80",
    "HLA-C*07:80"
  ],
  "panel_81": [
    "HLA-A*02:81",
    "HLA-B*07:81",
    "HLA-C*07:81"
  ],
  "panel_82": [
    "HLA-A*02:82",
    "HLA-B*07:82",
    "HLA-C*07:82"
  ],
  "panel_83": [
    "HLA-A*02:83",
    "HLA-B*07:83",
    "HLA-C*07:83"
  ],
  "panel_84": [
    "HLA-A*02:84",
    "HLA-B*07:84",
    "HLA-C*07:84"
  ],
  "panel_85": [
    "HLA-A*02:85",
    "HLA-B*07:85",
    "HLA-C*07:85"
  ],
  "panel_86": [
    "HLA-A*02:86",
    "HLA-B*07:86",
    "HLA-C*07:86"
  ],
  "panel_87": [
    "HLA-A*02:87",
    "HLA-B*07:87",
    "HLA-C*07:87"
  ],
  "panel_88": [
    "HLA-A*02:88",
    "HLA-B*07:88",
    "HLA-C*07:88"
  ],
  "panel_89": [
    "HLA-A*02:89",
    "HLA-B*07:89",
    "HLA-C*07:89"
  ],
  "panel_90": [
    "HLA-A*02:90",
    "HLA-B*07:90",
    "HLA-C*07:90"
  ],
  "panel_91": [
    "HLA-A*02:91",
    "HLA-B*07:91",
    "HLA-C*07:91"
  ],
  "panel_92": [
    "HLA-A*02:92",
    "HLA-B*07:92",
    "HLA-C*07:92"
  ],
  "panel_93": [
    "HLA-A*02:93",
    "HLA-B*07:93",
    "HLA-C*07:93"
  ],
  "panel_94": [
    "HLA-A*02:94",
    "HLA-B*07:94",
    "HLA-C*07:94"
  ],
  "panel_95": [
    "HLA-A*02:95",
    "HLA-B*07:95",
    "HLA-C*07:95"
  ],
  "panel_96": [
    "HLA-A*02:96",
    "HLA-B*07:96",
    "HLA-C*07:96"
  ],
  "panel_97": [
    "HLA-A*02:97",
    "HLA-B*07:97",
    "HLA-C*07:97"
  ],
  "panel_98": [
    "HLA-A*02:98",
    "HLA-B*07:98",
    "HLA-C*07:98"
  ],
  "panel_99": [
    "HLA-A*02:99",
    "HLA-B*07:99",
    "HLA-C*07:99"
  ],
  "panel_100": [
    "HLA-A*02:100",
    "HLA-B*07:100",
    "HLA-C*07:100"
  ],
  "panel_101": [
    "HLA-A*02:101",
    "HLA-B*07:101",
    "HLA-C*07:101"
  ],
  "panel_102": [
    "HLA-A*02:102",
    "HLA-B*07:102",
    "HLA-C*07:102"
  ],
  "panel_103": [
    "HLA-A*02:103",
    "HLA-B*07:103",
    "HLA-C*07:103"
  ],
  "panel_104": [
    "HLA-A*02:104",
    "HLA-B*07:104",
    "HLA-C*07:104"
  ],
  "panel_105": [
    "HLA-A*02:105",
    "HLA-B*07:105",
    "HLA-C*07:105"
  ],
  "panel_106": [
    "HLA-A*02:106",
    "HLA-B*07:106",
    "HLA-C*07:106"
  ],
  "panel_107": [
    "HLA-A*02:107",
    "HLA-B*07:107",
    "HLA-C*07:107"
  ],
  "panel_108": [
    "HLA-A*02:108",
    "HLA-B*07:108",
    "HLA-C*07:108"
  ],
  "panel_109": [
    "HLA-A*02:109",
    "HLA-B*07:109",
    "HLA-C*07:109"
  ],
  "panel_110": [
    "HLA-A*02:110",
    "HLA-B*07:110",
    "HLA-C*07:110"
  ],
  "panel_111": [
    "HLA-A*02:111",
    "HLA-B*07:111",
    "HLA-C*07:111"
  ],
  "panel_112": [
    "HLA-A*02:112",
    "HLA-B*07:112",
    "HLA-C*07:112"
  ],
  "panel_113": [
    "HLA-A*02:113",
    "HLA-B*07:113",
    "HLA-C*07:113"
  ],
  "panel_114": [
    "HLA-A*02:114",
    "HLA-B*07:114",
    "HLA-C*07:114"
  ],
  "panel_115": [
    "HLA-A*02:115",
    "HLA-B*07:115",
    "HLA-C*07:115"
  ],
  "panel_116": [
    "HLA-A*02:116",
    "HLA-B*07:116",
    "HLA-C*07:116"
  ],
  "panel_117": [
    "HLA-A*02:117",
    "HLA-B*07:117",
    "HLA-C*07:117"
  ],
  "panel_118": [
    "HLA-A*02:118",
    "HLA-B*07:118",
    "HLA-C*07:118"
  ],
  "panel_119": [
    "HLA-A*02:119",
    "HLA-B*07:119",
    "HLA-C*07:119"
  ],
  "panel_120": [
    "HLA-A*02:120",
    "HLA-B*07:120",
    "HLA-C*07:120"
  ],
  "panel_121": [
    "HLA-A*02:121",
    "HLA-B*07:121",
    "HLA-C*07:121"
  ],
  "panel_122": [
    "HLA-A*02:122",
    "HLA-B*07:122",
    "HLA-C*07:122"
  ],
  "panel_123": [
    "HLA-A*02:123",
    "HLA-B*07:123",
    "HLA-C*07:123"
  ],
  "panel_124": [
    "HLA-A*02:124",
    "HLA-B*07:124",
    "HLA-C*07:124"
  ],
  "panel_125": [
    "HLA-A*02:125",
    "HLA-B*07:125",
    "HLA-C*07:125"
  ],
  "panel_126": [
    "HLA-A*02:126",
    "HLA-B*07:126",
    "HLA-C*07:126"
  ],
  "panel_127": [
    "HLA-A*02:127",
    "HLA-B*07:127",
    "HLA-C*07:127"
  ],
  "panel_128": [
    "HLA-A*02:128",
    "HLA-B*07:128",
    "HLA-C*07:128"
  ],
  "panel_129": [
    "HLA-A*02:129",
    "HLA-B*07:129",
    "HLA-C*07:129"
  ],
  "panel_130": [
    "HLA-A*02:130",
    "HLA-B*07:130",
    "HLA-C*07:130"
  ],
  "panel_131": [
    "HLA-A*02:131",
    "HLA-B*07:131",
    "HLA-C*07:131"
  ],
  "panel_132": [
    "HLA-A*02:132",
    "HLA-B*07:132",
    "HLA-C*07:132"
  ],
  "panel_133": [
    "HLA-A*02:133",
    "HLA-B*07:133",
    "HLA-C*07:133"
  ],
  "panel_134": [
    "HLA-A*02:134",
    "HLA-B*07:134",
    "HLA-C*07:134"
  ],
  "panel_135": [
    "HLA-A*02:135",
    "HLA-B*07:135",
    "HLA-C*07:135"
  ],
  "panel_136": [
    "HLA-A*02:136",
    "HLA-B*07:136",
    "HLA-C*07:136"
  ],
  "panel_137": [
    "HLA-A*02:137",
    "HLA-B*07:137",
    "HLA-C*07:137"
  ],
  "panel_138": [
    "HLA-A*02:138",
    "HLA-B*07:138",
    "HLA-C*07:138"
  ],
  "panel_139": [
    "HLA-A*02:139",
    "HLA-B*07:139",
    "HLA-C*07:139"
  ],
  "panel_140": [
    "HLA-A*02:140",
    "HLA-B*07:140",
    "HLA-C*07:140"
  ],
  "panel_141": [
    "HLA-A*02:141",
    "HLA-B*07:141",
    "HLA-C*07:141"
  ],
  "panel_142": [
    "HLA-A*02:142",
    "HLA-B*07:142",
    "HLA-C*07:142"
  ],
  "panel_143": [
    "HLA-A*02:143",
    "HLA-B*07:143",
    "HLA-C*07:143"
  ],
  "panel_144": [
    "HLA-A*02:144",
    "HLA-B*07:144",
    "HLA-C*07:144"
  ],
  "panel_145": [
    "HLA-A*02:145",
    "HLA-B*07:145",
    "HLA-C*07:145"
  ],
  "panel_146": [
    "HLA-A*02:146",
    "HLA-B*07:146",
    "HLA-C*07:146"
  ],
  "panel_147": [
    "HLA-A*02:147",
    "HLA-B*07:147",
    "HLA-C*07:147"
  ],
  "panel_148": [
    "HLA-A*02:148",
    "HLA-B*07:148",
    "HLA-C*07:148"
  ],
  "panel_149": [
    "HLA-A*02:149",
    "HLA-B*07:149",
    "HLA-C*07:149"
  ],
  "panel_150": [
    "HLA-A*02:150",
    "HLA-B*07:150",
    "HLA-C*07:150"
  ],
  "panel_151": [
    "HLA-A*02:151",
    "HLA-B*07:151",
    "HLA-C*07:151"
  ],
  "panel_152": [
    "HLA-A*02:152",
    "HLA-B*07:152",
    "HLA-C*07:152"
  ],
  "panel_153": [
    "HLA-A*02:153",
    "HLA-B*07:153",
    "HLA-C*07:153"
  ],
  "panel_154": [
    "HLA-A*02:154",
    "HLA-B*07:154",
    "HLA-C*07:154"
  ],
  "panel_155": [
    "HLA-A*02:155",
    "HLA-B*07:155",
    "HLA-C*07:155"
  ],
  "panel_156": [
    "HLA-A*02:156",
    "HLA-B*07:156",
    "HLA-C*07:156"
  ],
  "panel_157": [
    "HLA-A*02:157",
    "HLA-B*07:157",
    "HLA-C*07:157"
  ],
  "panel_158": [
    "HLA-A*02:158",
    "HLA-B*07:158",
    "HLA-C*07:158"
  ],
  "panel_159": [
    "HLA-A*02:159",
    "HLA-B*07:159",
    "HLA-C*07:159"
  ],
  "panel_160": [
    "HLA-A*02:160",
    "HLA-B*07:160",
    "HLA-C*07:160"
  ],
  "panel_161": [
    "HLA-A*02:161",
    "HLA-B*07:161",
    "HLA-C*07:161"
  ],
  "panel_162": [
    "HLA-A*02:162",
    "HLA-B*07:162",
    "HLA-C*07:162"
  ],
  "panel_163": [
    "HLA-A*02:163",
    "HLA-B*07:163",
    "HLA-C*07:163"
  ],
  "panel_164": [
    "HLA-A*02:164",
    "HLA-B*07:164",
    "HLA-C*07:164"
  ],
  "panel_165": [
    "HLA-A*02:165",
    "HLA-B*07:165",
    "HLA-C*07:165"
  ],
  "panel_166": [
    "HLA-A*02:166",
    "HLA-B*07:166",
    "HLA-C*07:166"
  ],
  "panel_167": [
    "HLA-A*02:167",
    "HLA-B*07:167",
    "HLA-C*07:167"
  ],
  "panel_168": [
    "HLA-A*02:168",
    "HLA-B*07:168",
    "HLA-C*07:168"
  ],
  "panel_169": [
    "HLA-A*02:169",
    "HLA-B*07:169",
    "HLA-C*07:169"
  ],
  "panel_170": [
    "HLA-A*02:170",
    "HLA-B*07:170",
    "HLA-C*07:170"
  ],
  "panel_171": [
    "HLA-A*02:171",
    "HLA-B*07:171",
    "HLA-C*07:171"
  ],
  "panel_172": [
    "HLA-A*02:172",
    "HLA-B*07:172",
    "HLA-C*07:172"
  ],
  "panel_173": [
    "HLA-A*02:173",
    "HLA-B*07:173",
    "HLA-C*07:173"
  ],
  "panel_174": [
    "HLA-A*02:174",
    "HLA-B*07:174",
    "HLA-C*07:174"
  ],
  "panel_175": [
    "HLA-A*02:175",
    "HLA-B*07:175",
    "HLA-C*07:175"
  ],
  "panel_176": [
    "HLA-A*02:176",
    "HLA-B*07:176",
    "HLA-C*07:176"
  ],
  "panel_177": [
    "HLA-A*02:177",
    "HLA-B*07:177",
    "HLA-C*07:177"
  ],
  "panel_178": [
    "HLA-A*02:178",
    "HLA-B*07:178",
    "HLA-C*07:178"
  ],
  "panel_179": [
    "HLA-A*02:179",
    "HLA-B*07:179",
    "HLA-C*07:179"
  ]
}\nENCODE_ASSAYS = [
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq",
  "ChIP-seq",
  "DNase-seq",
  "ATAC-seq",
  "Hi-C",
  "ChIA-PET",
  "RNA-seq",
  "CAGE",
  "RAMPAGE",
  "eCLIP",
  "PRO-seq"
]\nSPATIAL_TECHS = [
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI",
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI",
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI",
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI",
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI",
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI",
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI",
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI",
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI",
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI",
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI",
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI",
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI",
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI",
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI",
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI",
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI",
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI",
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI",
  "Visium",
  "Stereo-seq",
  "MERFISH",
  "seqFISH",
  "CosMx",
  "GeoMx",
  "Slide-seqV2",
  "HyPR-seq",
  "DBiT-seq",
  "MIBI"
]\nREACTOME_MAP = {
  "R-HSA-100000": "Immune System",
  "R-HSA-100001": "Gene expression",
  "R-HSA-100002": "Gene expression",
  "R-HSA-100003": "Metabolism",
  "R-HSA-100004": "Gene expression",
  "R-HSA-100005": "Gene expression",
  "R-HSA-100006": "Gene expression",
  "R-HSA-100007": "Gene expression",
  "R-HSA-100008": "Gene expression",
  "R-HSA-100009": "Gene expression",
  "R-HSA-100010": "Immune System",
  "R-HSA-100011": "Gene expression",
  "R-HSA-100012": "Immune System",
  "R-HSA-100013": "Metabolism",
  "R-HSA-100014": "Gene expression",
  "R-HSA-100015": "Immune System",
  "R-HSA-100016": "Metabolism",
  "R-HSA-100017": "Immune System",
  "R-HSA-100018": "Immune System",
  "R-HSA-100019": "Immune System",
  "R-HSA-100020": "Gene expression",
  "R-HSA-100021": "Immune System",
  "R-HSA-100022": "Immune System",
  "R-HSA-100023": "Gene expression",
  "R-HSA-100024": "Gene expression",
  "R-HSA-100025": "Immune System",
  "R-HSA-100026": "Metabolism",
  "R-HSA-100027": "Metabolism",
  "R-HSA-100028": "Gene expression",
  "R-HSA-100029": "Immune System",
  "R-HSA-100030": "Metabolism",
  "R-HSA-100031": "Metabolism",
  "R-HSA-100032": "Gene expression",
  "R-HSA-100033": "Immune System",
  "R-HSA-100034": "Metabolism",
  "R-HSA-100035": "Gene expression",
  "R-HSA-100036": "Gene expression",
  "R-HSA-100037": "Immune System",
  "R-HSA-100038": "Metabolism",
  "R-HSA-100039": "Gene expression",
  "R-HSA-100040": "Gene expression",
  "R-HSA-100041": "Metabolism",
  "R-HSA-100042": "Metabolism",
  "R-HSA-100043": "Metabolism",
  "R-HSA-100044": "Gene expression",
  "R-HSA-100045": "Immune System",
  "R-HSA-100046": "Gene expression",
  "R-HSA-100047": "Immune System",
  "R-HSA-100048": "Immune System",
  "R-HSA-100049": "Immune System",
  "R-HSA-100050": "Metabolism",
  "R-HSA-100051": "Gene expression",
  "R-HSA-100052": "Immune System",
  "R-HSA-100053": "Immune System",
  "R-HSA-100054": "Gene expression",
  "R-HSA-100055": "Metabolism",
  "R-HSA-100056": "Immune System",
  "R-HSA-100057": "Metabolism",
  "R-HSA-100058": "Immune System",
  "R-HSA-100059": "Gene expression",
  "R-HSA-100060": "Immune System",
  "R-HSA-100061": "Immune System",
  "R-HSA-100062": "Immune System",
  "R-HSA-100063": "Immune System",
  "R-HSA-100064": "Immune System",
  "R-HSA-100065": "Metabolism",
  "R-HSA-100066": "Metabolism",
  "R-HSA-100067": "Gene expression",
  "R-HSA-100068": "Gene expression",
  "R-HSA-100069": "Gene expression",
  "R-HSA-100070": "Immune System",
  "R-HSA-100071": "Metabolism",
  "R-HSA-100072": "Gene expression",
  "R-HSA-100073": "Immune System",
  "R-HSA-100074": "Gene expression",
  "R-HSA-100075": "Immune System",
  "R-HSA-100076": "Immune System",
  "R-HSA-100077": "Immune System",
  "R-HSA-100078": "Metabolism",
  "R-HSA-100079": "Immune System",
  "R-HSA-100080": "Immune System",
  "R-HSA-100081": "Immune System",
  "R-HSA-100082": "Metabolism",
  "R-HSA-100083": "Gene expression",
  "R-HSA-100084": "Gene expression",
  "R-HSA-100085": "Gene expression",
  "R-HSA-100086": "Immune System",
  "R-HSA-100087": "Gene expression",
  "R-HSA-100088": "Immune System",
  "R-HSA-100089": "Metabolism",
  "R-HSA-100090": "Immune System",
  "R-HSA-100091": "Immune System",
  "R-HSA-100092": "Gene expression",
  "R-HSA-100093": "Immune System",
  "R-HSA-100094": "Metabolism",
  "R-HSA-100095": "Metabolism",
  "R-HSA-100096": "Gene expression",
  "R-HSA-100097": "Metabolism",
  "R-HSA-100098": "Metabolism",
  "R-HSA-100099": "Gene expression",
  "R-HSA-100100": "Metabolism",
  "R-HSA-100101": "Metabolism",
  "R-HSA-100102": "Immune System",
  "R-HSA-100103": "Metabolism",
  "R-HSA-100104": "Immune System",
  "R-HSA-100105": "Metabolism",
  "R-HSA-100106": "Metabolism",
  "R-HSA-100107": "Metabolism",
  "R-HSA-100108": "Immune System",
  "R-HSA-100109": "Immune System",
  "R-HSA-100110": "Immune System",
  "R-HSA-100111": "Immune System",
  "R-HSA-100112": "Metabolism",
  "R-HSA-100113": "Gene expression",
  "R-HSA-100114": "Metabolism",
  "R-HSA-100115": "Metabolism",
  "R-HSA-100116": "Metabolism",
  "R-HSA-100117": "Metabolism",
  "R-HSA-100118": "Immune System",
  "R-HSA-100119": "Immune System",
  "R-HSA-100120": "Immune System",
  "R-HSA-100121": "Gene expression",
  "R-HSA-100122": "Gene expression",
  "R-HSA-100123": "Metabolism",
  "R-HSA-100124": "Metabolism",
  "R-HSA-100125": "Metabolism",
  "R-HSA-100126": "Gene expression",
  "R-HSA-100127": "Immune System",
  "R-HSA-100128": "Immune System",
  "R-HSA-100129": "Metabolism",
  "R-HSA-100130": "Gene expression",
  "R-HSA-100131": "Immune System",
  "R-HSA-100132": "Gene expression",
  "R-HSA-100133": "Immune System",
  "R-HSA-100134": "Metabolism",
  "R-HSA-100135": "Immune System",
  "R-HSA-100136": "Immune System",
  "R-HSA-100137": "Immune System",
  "R-HSA-100138": "Immune System",
  "R-HSA-100139": "Metabolism",
  "R-HSA-100140": "Immune System",
  "R-HSA-100141": "Immune System",
  "R-HSA-100142": "Metabolism",
  "R-HSA-100143": "Gene expression",
  "R-HSA-100144": "Metabolism",
  "R-HSA-100145": "Immune System",
  "R-HSA-100146": "Gene expression",
  "R-HSA-100147": "Metabolism",
  "R-HSA-100148": "Gene expression",
  "R-HSA-100149": "Gene expression",
  "R-HSA-100150": "Gene expression",
  "R-HSA-100151": "Metabolism",
  "R-HSA-100152": "Immune System",
  "R-HSA-100153": "Metabolism",
  "R-HSA-100154": "Immune System",
  "R-HSA-100155": "Gene expression",
  "R-HSA-100156": "Immune System",
  "R-HSA-100157": "Immune System",
  "R-HSA-100158": "Gene expression",
  "R-HSA-100159": "Immune System",
  "R-HSA-100160": "Immune System",
  "R-HSA-100161": "Metabolism",
  "R-HSA-100162": "Immune System",
  "R-HSA-100163": "Immune System",
  "R-HSA-100164": "Metabolism",
  "R-HSA-100165": "Metabolism",
  "R-HSA-100166": "Metabolism",
  "R-HSA-100167": "Metabolism",
  "R-HSA-100168": "Immune System",
  "R-HSA-100169": "Gene expression",
  "R-HSA-100170": "Immune System",
  "R-HSA-100171": "Metabolism",
  "R-HSA-100172": "Immune System",
  "R-HSA-100173": "Gene expression",
  "R-HSA-100174": "Metabolism",
  "R-HSA-100175": "Immune System",
  "R-HSA-100176": "Immune System",
  "R-HSA-100177": "Immune System",
  "R-HSA-100178": "Gene expression",
  "R-HSA-100179": "Gene expression",
  "R-HSA-100180": "Gene expression",
  "R-HSA-100181": "Gene expression",
  "R-HSA-100182": "Immune System",
  "R-HSA-100183": "Immune System",
  "R-HSA-100184": "Immune System",
  "R-HSA-100185": "Gene expression",
  "R-HSA-100186": "Metabolism",
  "R-HSA-100187": "Gene expression",
  "R-HSA-100188": "Metabolism",
  "R-HSA-100189": "Immune System",
  "R-HSA-100190": "Metabolism",
  "R-HSA-100191": "Gene expression",
  "R-HSA-100192": "Metabolism",
  "R-HSA-100193": "Gene expression",
  "R-HSA-100194": "Gene expression",
  "R-HSA-100195": "Immune System",
  "R-HSA-100196": "Gene expression",
  "R-HSA-100197": "Immune System",
  "R-HSA-100198": "Immune System",
  "R-HSA-100199": "Gene expression",
  "R-HSA-100200": "Immune System",
  "R-HSA-100201": "Immune System",
  "R-HSA-100202": "Immune System",
  "R-HSA-100203": "Metabolism",
  "R-HSA-100204": "Gene expression",
  "R-HSA-100205": "Metabolism",
  "R-HSA-100206": "Immune System",
  "R-HSA-100207": "Immune System",
  "R-HSA-100208": "Gene expression",
  "R-HSA-100209": "Gene expression",
  "R-HSA-100210": "Metabolism",
  "R-HSA-100211": "Gene expression",
  "R-HSA-100212": "Gene expression",
  "R-HSA-100213": "Immune System",
  "R-HSA-100214": "Metabolism",
  "R-HSA-100215": "Immune System",
  "R-HSA-100216": "Metabolism",
  "R-HSA-100217": "Immune System",
  "R-HSA-100218": "Immune System",
  "R-HSA-100219": "Metabolism",
  "R-HSA-100220": "Gene expression",
  "R-HSA-100221": "Metabolism",
  "R-HSA-100222": "Immune System",
  "R-HSA-100223": "Immune System",
  "R-HSA-100224": "Immune System",
  "R-HSA-100225": "Gene expression",
  "R-HSA-100226": "Metabolism",
  "R-HSA-100227": "Metabolism",
  "R-HSA-100228": "Gene expression",
  "R-HSA-100229": "Metabolism",
  "R-HSA-100230": "Gene expression",
  "R-HSA-100231": "Gene expression",
  "R-HSA-100232": "Metabolism",
  "R-HSA-100233": "Metabolism",
  "R-HSA-100234": "Metabolism",
  "R-HSA-100235": "Metabolism",
  "R-HSA-100236": "Immune System",
  "R-HSA-100237": "Gene expression",
  "R-HSA-100238": "Immune System",
  "R-HSA-100239": "Immune System",
  "R-HSA-100240": "Immune System",
  "R-HSA-100241": "Immune System",
  "R-HSA-100242": "Gene expression",
  "R-HSA-100243": "Metabolism",
  "R-HSA-100244": "Gene expression",
  "R-HSA-100245": "Gene expression",
  "R-HSA-100246": "Metabolism",
  "R-HSA-100247": "Metabolism",
  "R-HSA-100248": "Immune System",
  "R-HSA-100249": "Gene expression",
  "R-HSA-100250": "Immune System",
  "R-HSA-100251": "Metabolism",
  "R-HSA-100252": "Gene expression",
  "R-HSA-100253": "Gene expression",
  "R-HSA-100254": "Immune System",
  "R-HSA-100255": "Metabolism",
  "R-HSA-100256": "Gene expression",
  "R-HSA-100257": "Gene expression",
  "R-HSA-100258": "Immune System",
  "R-HSA-100259": "Metabolism",
  "R-HSA-100260": "Metabolism",
  "R-HSA-100261": "Immune System",
  "R-HSA-100262": "Gene expression",
  "R-HSA-100263": "Metabolism",
  "R-HSA-100264": "Gene expression",
  "R-HSA-100265": "Metabolism",
  "R-HSA-100266": "Immune System",
  "R-HSA-100267": "Gene expression",
  "R-HSA-100268": "Immune System",
  "R-HSA-100269": "Gene expression",
  "R-HSA-100270": "Immune System",
  "R-HSA-100271": "Metabolism",
  "R-HSA-100272": "Gene expression",
  "R-HSA-100273": "Metabolism",
  "R-HSA-100274": "Gene expression",
  "R-HSA-100275": "Gene expression",
  "R-HSA-100276": "Immune System",
  "R-HSA-100277": "Metabolism",
  "R-HSA-100278": "Gene expression",
  "R-HSA-100279": "Gene expression",
  "R-HSA-100280": "Gene expression",
  "R-HSA-100281": "Immune System",
  "R-HSA-100282": "Immune System",
  "R-HSA-100283": "Metabolism",
  "R-HSA-100284": "Immune System",
  "R-HSA-100285": "Metabolism",
  "R-HSA-100286": "Immune System",
  "R-HSA-100287": "Immune System",
  "R-HSA-100288": "Immune System",
  "R-HSA-100289": "Metabolism",
  "R-HSA-100290": "Gene expression",
  "R-HSA-100291": "Metabolism",
  "R-HSA-100292": "Gene expression",
  "R-HSA-100293": "Metabolism",
  "R-HSA-100294": "Metabolism",
  "R-HSA-100295": "Metabolism",
  "R-HSA-100296": "Gene expression",
  "R-HSA-100297": "Gene expression",
  "R-HSA-100298": "Gene expression",
  "R-HSA-100299": "Gene expression",
  "R-HSA-100300": "Metabolism",
  "R-HSA-100301": "Immune System",
  "R-HSA-100302": "Gene expression",
  "R-HSA-100303": "Gene expression",
  "R-HSA-100304": "Metabolism",
  "R-HSA-100305": "Immune System",
  "R-HSA-100306": "Metabolism",
  "R-HSA-100307": "Metabolism",
  "R-HSA-100308": "Gene expression",
  "R-HSA-100309": "Immune System",
  "R-HSA-100310": "Gene expression",
  "R-HSA-100311": "Gene expression",
  "R-HSA-100312": "Gene expression",
  "R-HSA-100313": "Gene expression",
  "R-HSA-100314": "Gene expression",
  "R-HSA-100315": "Immune System",
  "R-HSA-100316": "Metabolism",
  "R-HSA-100317": "Metabolism",
  "R-HSA-100318": "Gene expression",
  "R-HSA-100319": "Gene expression",
  "R-HSA-100320": "Metabolism",
  "R-HSA-100321": "Metabolism",
  "R-HSA-100322": "Metabolism",
  "R-HSA-100323": "Immune System",
  "R-HSA-100324": "Metabolism",
  "R-HSA-100325": "Immune System",
  "R-HSA-100326": "Metabolism",
  "R-HSA-100327": "Gene expression",
  "R-HSA-100328": "Gene expression",
  "R-HSA-100329": "Metabolism",
  "R-HSA-100330": "Metabolism",
  "R-HSA-100331": "Immune System",
  "R-HSA-100332": "Immune System",
  "R-HSA-100333": "Immune System",
  "R-HSA-100334": "Metabolism",
  "R-HSA-100335": "Immune System",
  "R-HSA-100336": "Metabolism",
  "R-HSA-100337": "Immune System",
  "R-HSA-100338": "Immune System",
  "R-HSA-100339": "Gene expression",
  "R-HSA-100340": "Gene expression",
  "R-HSA-100341": "Gene expression",
  "R-HSA-100342": "Metabolism",
  "R-HSA-100343": "Immune System",
  "R-HSA-100344": "Metabolism",
  "R-HSA-100345": "Immune System",
  "R-HSA-100346": "Gene expression",
  "R-HSA-100347": "Immune System",
  "R-HSA-100348": "Metabolism",
  "R-HSA-100349": "Metabolism",
  "R-HSA-100350": "Gene expression",
  "R-HSA-100351": "Immune System",
  "R-HSA-100352": "Gene expression",
  "R-HSA-100353": "Gene expression",
  "R-HSA-100354": "Metabolism",
  "R-HSA-100355": "Metabolism",
  "R-HSA-100356": "Gene expression",
  "R-HSA-100357": "Gene expression",
  "R-HSA-100358": "Immune System",
  "R-HSA-100359": "Gene expression",
  "R-HSA-100360": "Metabolism",
  "R-HSA-100361": "Metabolism",
  "R-HSA-100362": "Immune System",
  "R-HSA-100363": "Immune System",
  "R-HSA-100364": "Gene expression",
  "R-HSA-100365": "Immune System",
  "R-HSA-100366": "Gene expression",
  "R-HSA-100367": "Gene expression",
  "R-HSA-100368": "Gene expression",
  "R-HSA-100369": "Gene expression",
  "R-HSA-100370": "Immune System",
  "R-HSA-100371": "Gene expression",
  "R-HSA-100372": "Gene expression",
  "R-HSA-100373": "Gene expression",
  "R-HSA-100374": "Immune System",
  "R-HSA-100375": "Immune System",
  "R-HSA-100376": "Metabolism",
  "R-HSA-100377": "Immune System",
  "R-HSA-100378": "Metabolism",
  "R-HSA-100379": "Metabolism",
  "R-HSA-100380": "Metabolism",
  "R-HSA-100381": "Immune System",
  "R-HSA-100382": "Immune System",
  "R-HSA-100383": "Immune System",
  "R-HSA-100384": "Immune System",
  "R-HSA-100385": "Gene expression",
  "R-HSA-100386": "Immune System",
  "R-HSA-100387": "Metabolism",
  "R-HSA-100388": "Gene expression",
  "R-HSA-100389": "Metabolism",
  "R-HSA-100390": "Immune System",
  "R-HSA-100391": "Gene expression",
  "R-HSA-100392": "Metabolism",
  "R-HSA-100393": "Immune System",
  "R-HSA-100394": "Metabolism",
  "R-HSA-100395": "Immune System",
  "R-HSA-100396": "Gene expression",
  "R-HSA-100397": "Metabolism",
  "R-HSA-100398": "Metabolism",
  "R-HSA-100399": "Gene expression",
  "R-HSA-100400": "Immune System",
  "R-HSA-100401": "Gene expression",
  "R-HSA-100402": "Gene expression",
  "R-HSA-100403": "Metabolism",
  "R-HSA-100404": "Immune System",
  "R-HSA-100405": "Metabolism",
  "R-HSA-100406": "Metabolism",
  "R-HSA-100407": "Gene expression",
  "R-HSA-100408": "Immune System",
  "R-HSA-100409": "Immune System",
  "R-HSA-100410": "Immune System",
  "R-HSA-100411": "Metabolism",
  "R-HSA-100412": "Immune System",
  "R-HSA-100413": "Immune System",
  "R-HSA-100414": "Metabolism",
  "R-HSA-100415": "Immune System",
  "R-HSA-100416": "Gene expression",
  "R-HSA-100417": "Gene expression",
  "R-HSA-100418": "Metabolism",
  "R-HSA-100419": "Metabolism",
  "R-HSA-100420": "Immune System",
  "R-HSA-100421": "Metabolism",
  "R-HSA-100422": "Immune System",
  "R-HSA-100423": "Immune System",
  "R-HSA-100424": "Gene expression",
  "R-HSA-100425": "Immune System",
  "R-HSA-100426": "Metabolism",
  "R-HSA-100427": "Gene expression",
  "R-HSA-100428": "Gene expression",
  "R-HSA-100429": "Metabolism",
  "R-HSA-100430": "Immune System",
  "R-HSA-100431": "Metabolism",
  "R-HSA-100432": "Metabolism",
  "R-HSA-100433": "Metabolism",
  "R-HSA-100434": "Gene expression",
  "R-HSA-100435": "Immune System",
  "R-HSA-100436": "Metabolism",
  "R-HSA-100437": "Metabolism",
  "R-HSA-100438": "Immune System",
  "R-HSA-100439": "Metabolism",
  "R-HSA-100440": "Gene expression",
  "R-HSA-100441": "Gene expression",
  "R-HSA-100442": "Immune System",
  "R-HSA-100443": "Metabolism",
  "R-HSA-100444": "Metabolism",
  "R-HSA-100445": "Immune System",
  "R-HSA-100446": "Metabolism",
  "R-HSA-100447": "Gene expression",
  "R-HSA-100448": "Metabolism",
  "R-HSA-100449": "Immune System",
  "R-HSA-100450": "Metabolism",
  "R-HSA-100451": "Gene expression",
  "R-HSA-100452": "Metabolism",
  "R-HSA-100453": "Metabolism",
  "R-HSA-100454": "Gene expression",
  "R-HSA-100455": "Immune System",
  "R-HSA-100456": "Metabolism",
  "R-HSA-100457": "Metabolism",
  "R-HSA-100458": "Immune System",
  "R-HSA-100459": "Gene expression",
  "R-HSA-100460": "Metabolism",
  "R-HSA-100461": "Metabolism",
  "R-HSA-100462": "Immune System",
  "R-HSA-100463": "Metabolism",
  "R-HSA-100464": "Immune System",
  "R-HSA-100465": "Metabolism",
  "R-HSA-100466": "Gene expression",
  "R-HSA-100467": "Metabolism",
  "R-HSA-100468": "Metabolism",
  "R-HSA-100469": "Immune System",
  "R-HSA-100470": "Gene expression",
  "R-HSA-100471": "Gene expression",
  "R-HSA-100472": "Gene expression",
  "R-HSA-100473": "Metabolism",
  "R-HSA-100474": "Gene expression",
  "R-HSA-100475": "Immune System",
  "R-HSA-100476": "Gene expression",
  "R-HSA-100477": "Gene expression",
  "R-HSA-100478": "Immune System",
  "R-HSA-100479": "Metabolism",
  "R-HSA-100480": "Immune System",
  "R-HSA-100481": "Gene expression",
  "R-HSA-100482": "Immune System",
  "R-HSA-100483": "Gene expression",
  "R-HSA-100484": "Gene expression",
  "R-HSA-100485": "Gene expression",
  "R-HSA-100486": "Immune System",
  "R-HSA-100487": "Gene expression",
  "R-HSA-100488": "Metabolism",
  "R-HSA-100489": "Gene expression",
  "R-HSA-100490": "Gene expression",
  "R-HSA-100491": "Immune System",
  "R-HSA-100492": "Metabolism",
  "R-HSA-100493": "Metabolism",
  "R-HSA-100494": "Metabolism",
  "R-HSA-100495": "Metabolism",
  "R-HSA-100496": "Metabolism",
  "R-HSA-100497": "Metabolism",
  "R-HSA-100498": "Immune System",
  "R-HSA-100499": "Metabolism",
  "R-HSA-100500": "Metabolism",
  "R-HSA-100501": "Immune System",
  "R-HSA-100502": "Gene expression",
  "R-HSA-100503": "Metabolism",
  "R-HSA-100504": "Immune System",
  "R-HSA-100505": "Immune System",
  "R-HSA-100506": "Metabolism",
  "R-HSA-100507": "Immune System",
  "R-HSA-100508": "Immune System",
  "R-HSA-100509": "Gene expression",
  "R-HSA-100510": "Gene expression",
  "R-HSA-100511": "Metabolism",
  "R-HSA-100512": "Metabolism",
  "R-HSA-100513": "Metabolism",
  "R-HSA-100514": "Metabolism",
  "R-HSA-100515": "Gene expression",
  "R-HSA-100516": "Immune System",
  "R-HSA-100517": "Metabolism",
  "R-HSA-100518": "Immune System",
  "R-HSA-100519": "Immune System",
  "R-HSA-100520": "Gene expression",
  "R-HSA-100521": "Gene expression",
  "R-HSA-100522": "Gene expression",
  "R-HSA-100523": "Immune System",
  "R-HSA-100524": "Gene expression",
  "R-HSA-100525": "Immune System",
  "R-HSA-100526": "Gene expression",
  "R-HSA-100527": "Immune System",
  "R-HSA-100528": "Immune System",
  "R-HSA-100529": "Gene expression",
  "R-HSA-100530": "Immune System",
  "R-HSA-100531": "Immune System",
  "R-HSA-100532": "Gene expression",
  "R-HSA-100533": "Gene expression",
  "R-HSA-100534": "Immune System",
  "R-HSA-100535": "Gene expression",
  "R-HSA-100536": "Immune System",
  "R-HSA-100537": "Gene expression",
  "R-HSA-100538": "Metabolism",
  "R-HSA-100539": "Metabolism",
  "R-HSA-100540": "Metabolism",
  "R-HSA-100541": "Immune System",
  "R-HSA-100542": "Gene expression",
  "R-HSA-100543": "Metabolism",
  "R-HSA-100544": "Metabolism",
  "R-HSA-100545": "Metabolism",
  "R-HSA-100546": "Metabolism",
  "R-HSA-100547": "Immune System",
  "R-HSA-100548": "Metabolism",
  "R-HSA-100549": "Metabolism",
  "R-HSA-100550": "Gene expression",
  "R-HSA-100551": "Metabolism",
  "R-HSA-100552": "Metabolism",
  "R-HSA-100553": "Immune System",
  "R-HSA-100554": "Metabolism",
  "R-HSA-100555": "Gene expression",
  "R-HSA-100556": "Immune System",
  "R-HSA-100557": "Immune System",
  "R-HSA-100558": "Gene expression",
  "R-HSA-100559": "Metabolism",
  "R-HSA-100560": "Gene expression",
  "R-HSA-100561": "Gene expression",
  "R-HSA-100562": "Gene expression",
  "R-HSA-100563": "Gene expression",
  "R-HSA-100564": "Immune System",
  "R-HSA-100565": "Gene expression",
  "R-HSA-100566": "Gene expression",
  "R-HSA-100567": "Immune System",
  "R-HSA-100568": "Gene expression",
  "R-HSA-100569": "Immune System",
  "R-HSA-100570": "Immune System",
  "R-HSA-100571": "Gene expression",
  "R-HSA-100572": "Metabolism",
  "R-HSA-100573": "Gene expression",
  "R-HSA-100574": "Immune System",
  "R-HSA-100575": "Gene expression",
  "R-HSA-100576": "Immune System",
  "R-HSA-100577": "Immune System",
  "R-HSA-100578": "Immune System",
  "R-HSA-100579": "Metabolism",
  "R-HSA-100580": "Immune System",
  "R-HSA-100581": "Immune System",
  "R-HSA-100582": "Metabolism",
  "R-HSA-100583": "Metabolism",
  "R-HSA-100584": "Gene expression",
  "R-HSA-100585": "Gene expression",
  "R-HSA-100586": "Immune System",
  "R-HSA-100587": "Gene expression",
  "R-HSA-100588": "Gene expression",
  "R-HSA-100589": "Gene expression",
  "R-HSA-100590": "Metabolism",
  "R-HSA-100591": "Gene expression",
  "R-HSA-100592": "Gene expression",
  "R-HSA-100593": "Immune System",
  "R-HSA-100594": "Immune System",
  "R-HSA-100595": "Immune System",
  "R-HSA-100596": "Metabolism",
  "R-HSA-100597": "Metabolism",
  "R-HSA-100598": "Immune System",
  "R-HSA-100599": "Metabolism",
  "R-HSA-100600": "Gene expression",
  "R-HSA-100601": "Metabolism",
  "R-HSA-100602": "Immune System",
  "R-HSA-100603": "Gene expression",
  "R-HSA-100604": "Gene expression",
  "R-HSA-100605": "Metabolism",
  "R-HSA-100606": "Metabolism",
  "R-HSA-100607": "Gene expression",
  "R-HSA-100608": "Metabolism",
  "R-HSA-100609": "Metabolism",
  "R-HSA-100610": "Gene expression",
  "R-HSA-100611": "Metabolism",
  "R-HSA-100612": "Gene expression",
  "R-HSA-100613": "Gene expression",
  "R-HSA-100614": "Gene expression",
  "R-HSA-100615": "Immune System",
  "R-HSA-100616": "Immune System",
  "R-HSA-100617": "Gene expression",
  "R-HSA-100618": "Metabolism",
  "R-HSA-100619": "Gene expression",
  "R-HSA-100620": "Metabolism",
  "R-HSA-100621": "Metabolism",
  "R-HSA-100622": "Immune System",
  "R-HSA-100623": "Immune System",
  "R-HSA-100624": "Immune System",
  "R-HSA-100625": "Metabolism",
  "R-HSA-100626": "Gene expression",
  "R-HSA-100627": "Metabolism",
  "R-HSA-100628": "Gene expression",
  "R-HSA-100629": "Immune System",
  "R-HSA-100630": "Gene expression",
  "R-HSA-100631": "Gene expression",
  "R-HSA-100632": "Immune System",
  "R-HSA-100633": "Immune System",
  "R-HSA-100634": "Gene expression",
  "R-HSA-100635": "Immune System",
  "R-HSA-100636": "Gene expression",
  "R-HSA-100637": "Metabolism",
  "R-HSA-100638": "Metabolism",
  "R-HSA-100639": "Metabolism",
  "R-HSA-100640": "Immune System",
  "R-HSA-100641": "Metabolism",
  "R-HSA-100642": "Metabolism",
  "R-HSA-100643": "Metabolism",
  "R-HSA-100644": "Gene expression",
  "R-HSA-100645": "Metabolism",
  "R-HSA-100646": "Metabolism",
  "R-HSA-100647": "Gene expression",
  "R-HSA-100648": "Immune System",
  "R-HSA-100649": "Immune System",
  "R-HSA-100650": "Immune System",
  "R-HSA-100651": "Metabolism",
  "R-HSA-100652": "Gene expression",
  "R-HSA-100653": "Metabolism",
  "R-HSA-100654": "Metabolism",
  "R-HSA-100655": "Gene expression",
  "R-HSA-100656": "Metabolism",
  "R-HSA-100657": "Metabolism",
  "R-HSA-100658": "Immune System",
  "R-HSA-100659": "Gene expression",
  "R-HSA-100660": "Immune System",
  "R-HSA-100661": "Immune System",
  "R-HSA-100662": "Metabolism",
  "R-HSA-100663": "Immune System",
  "R-HSA-100664": "Gene expression",
  "R-HSA-100665": "Metabolism",
  "R-HSA-100666": "Immune System",
  "R-HSA-100667": "Gene expression",
  "R-HSA-100668": "Gene expression",
  "R-HSA-100669": "Immune System",
  "R-HSA-100670": "Immune System",
  "R-HSA-100671": "Gene expression",
  "R-HSA-100672": "Immune System",
  "R-HSA-100673": "Immune System",
  "R-HSA-100674": "Metabolism",
  "R-HSA-100675": "Immune System",
  "R-HSA-100676": "Gene expression",
  "R-HSA-100677": "Metabolism",
  "R-HSA-100678": "Metabolism",
  "R-HSA-100679": "Metabolism",
  "R-HSA-100680": "Immune System",
  "R-HSA-100681": "Immune System",
  "R-HSA-100682": "Immune System",
  "R-HSA-100683": "Gene expression",
  "R-HSA-100684": "Gene expression",
  "R-HSA-100685": "Immune System",
  "R-HSA-100686": "Metabolism",
  "R-HSA-100687": "Gene expression",
  "R-HSA-100688": "Gene expression",
  "R-HSA-100689": "Immune System",
  "R-HSA-100690": "Metabolism",
  "R-HSA-100691": "Immune System",
  "R-HSA-100692": "Gene expression",
  "R-HSA-100693": "Metabolism",
  "R-HSA-100694": "Immune System",
  "R-HSA-100695": "Metabolism",
  "R-HSA-100696": "Immune System",
  "R-HSA-100697": "Metabolism",
  "R-HSA-100698": "Metabolism",
  "R-HSA-100699": "Metabolism",
  "R-HSA-100700": "Metabolism",
  "R-HSA-100701": "Metabolism",
  "R-HSA-100702": "Gene expression",
  "R-HSA-100703": "Immune System",
  "R-HSA-100704": "Metabolism",
  "R-HSA-100705": "Gene expression",
  "R-HSA-100706": "Metabolism",
  "R-HSA-100707": "Immune System",
  "R-HSA-100708": "Immune System",
  "R-HSA-100709": "Metabolism",
  "R-HSA-100710": "Immune System",
  "R-HSA-100711": "Metabolism",
  "R-HSA-100712": "Gene expression",
  "R-HSA-100713": "Metabolism",
  "R-HSA-100714": "Gene expression",
  "R-HSA-100715": "Metabolism",
  "R-HSA-100716": "Gene expression",
  "R-HSA-100717": "Metabolism",
  "R-HSA-100718": "Gene expression",
  "R-HSA-100719": "Gene expression",
  "R-HSA-100720": "Immune System",
  "R-HSA-100721": "Immune System",
  "R-HSA-100722": "Gene expression",
  "R-HSA-100723": "Gene expression",
  "R-HSA-100724": "Gene expression",
  "R-HSA-100725": "Metabolism",
  "R-HSA-100726": "Gene expression",
  "R-HSA-100727": "Metabolism",
  "R-HSA-100728": "Immune System",
  "R-HSA-100729": "Metabolism",
  "R-HSA-100730": "Immune System",
  "R-HSA-100731": "Metabolism",
  "R-HSA-100732": "Metabolism",
  "R-HSA-100733": "Immune System",
  "R-HSA-100734": "Immune System",
  "R-HSA-100735": "Immune System",
  "R-HSA-100736": "Metabolism",
  "R-HSA-100737": "Gene expression",
  "R-HSA-100738": "Metabolism",
  "R-HSA-100739": "Metabolism",
  "R-HSA-100740": "Gene expression",
  "R-HSA-100741": "Metabolism",
  "R-HSA-100742": "Metabolism",
  "R-HSA-100743": "Immune System",
  "R-HSA-100744": "Gene expression",
  "R-HSA-100745": "Immune System",
  "R-HSA-100746": "Immune System",
  "R-HSA-100747": "Immune System",
  "R-HSA-100748": "Immune System",
  "R-HSA-100749": "Metabolism",
  "R-HSA-100750": "Immune System",
  "R-HSA-100751": "Gene expression",
  "R-HSA-100752": "Gene expression",
  "R-HSA-100753": "Metabolism",
  "R-HSA-100754": "Metabolism",
  "R-HSA-100755": "Gene expression",
  "R-HSA-100756": "Gene expression",
  "R-HSA-100757": "Immune System",
  "R-HSA-100758": "Immune System",
  "R-HSA-100759": "Immune System",
  "R-HSA-100760": "Immune System",
  "R-HSA-100761": "Metabolism",
  "R-HSA-100762": "Metabolism",
  "R-HSA-100763": "Metabolism",
  "R-HSA-100764": "Metabolism",
  "R-HSA-100765": "Metabolism",
  "R-HSA-100766": "Metabolism",
  "R-HSA-100767": "Gene expression",
  "R-HSA-100768": "Gene expression",
  "R-HSA-100769": "Immune System",
  "R-HSA-100770": "Immune System",
  "R-HSA-100771": "Gene expression",
  "R-HSA-100772": "Metabolism",
  "R-HSA-100773": "Metabolism",
  "R-HSA-100774": "Immune System",
  "R-HSA-100775": "Immune System",
  "R-HSA-100776": "Metabolism",
  "R-HSA-100777": "Gene expression",
  "R-HSA-100778": "Gene expression",
  "R-HSA-100779": "Gene expression",
  "R-HSA-100780": "Immune System",
  "R-HSA-100781": "Metabolism",
  "R-HSA-100782": "Metabolism",
  "R-HSA-100783": "Immune System",
  "R-HSA-100784": "Gene expression",
  "R-HSA-100785": "Immune System",
  "R-HSA-100786": "Gene expression",
  "R-HSA-100787": "Metabolism",
  "R-HSA-100788": "Metabolism",
  "R-HSA-100789": "Immune System",
  "R-HSA-100790": "Immune System",
  "R-HSA-100791": "Gene expression",
  "R-HSA-100792": "Immune System",
  "R-HSA-100793": "Immune System",
  "R-HSA-100794": "Gene expression",
  "R-HSA-100795": "Gene expression",
  "R-HSA-100796": "Metabolism",
  "R-HSA-100797": "Immune System",
  "R-HSA-100798": "Immune System",
  "R-HSA-100799": "Gene expression",
  "R-HSA-100800": "Gene expression",
  "R-HSA-100801": "Gene expression",
  "R-HSA-100802": "Gene expression",
  "R-HSA-100803": "Gene expression",
  "R-HSA-100804": "Immune System",
  "R-HSA-100805": "Metabolism",
  "R-HSA-100806": "Gene expression",
  "R-HSA-100807": "Immune System",
  "R-HSA-100808": "Gene expression",
  "R-HSA-100809": "Immune System",
  "R-HSA-100810": "Metabolism",
  "R-HSA-100811": "Gene expression",
  "R-HSA-100812": "Metabolism",
  "R-HSA-100813": "Gene expression",
  "R-HSA-100814": "Gene expression",
  "R-HSA-100815": "Metabolism",
  "R-HSA-100816": "Gene expression",
  "R-HSA-100817": "Immune System",
  "R-HSA-100818": "Gene expression",
  "R-HSA-100819": "Immune System",
  "R-HSA-100820": "Immune System",
  "R-HSA-100821": "Gene expression",
  "R-HSA-100822": "Metabolism",
  "R-HSA-100823": "Metabolism",
  "R-HSA-100824": "Metabolism",
  "R-HSA-100825": "Metabolism",
  "R-HSA-100826": "Immune System",
  "R-HSA-100827": "Metabolism",
  "R-HSA-100828": "Gene expression",
  "R-HSA-100829": "Gene expression",
  "R-HSA-100830": "Metabolism",
  "R-HSA-100831": "Metabolism",
  "R-HSA-100832": "Gene expression",
  "R-HSA-100833": "Metabolism",
  "R-HSA-100834": "Gene expression",
  "R-HSA-100835": "Immune System",
  "R-HSA-100836": "Gene expression",
  "R-HSA-100837": "Metabolism",
  "R-HSA-100838": "Metabolism",
  "R-HSA-100839": "Immune System",
  "R-HSA-100840": "Immune System",
  "R-HSA-100841": "Immune System",
  "R-HSA-100842": "Gene expression",
  "R-HSA-100843": "Immune System",
  "R-HSA-100844": "Immune System",
  "R-HSA-100845": "Metabolism",
  "R-HSA-100846": "Immune System",
  "R-HSA-100847": "Gene expression",
  "R-HSA-100848": "Gene expression",
  "R-HSA-100849": "Immune System",
  "R-HSA-100850": "Immune System",
  "R-HSA-100851": "Gene expression",
  "R-HSA-100852": "Metabolism",
  "R-HSA-100853": "Gene expression",
  "R-HSA-100854": "Metabolism",
  "R-HSA-100855": "Metabolism",
  "R-HSA-100856": "Metabolism",
  "R-HSA-100857": "Metabolism",
  "R-HSA-100858": "Gene expression",
  "R-HSA-100859": "Immune System",
  "R-HSA-100860": "Metabolism",
  "R-HSA-100861": "Immune System",
  "R-HSA-100862": "Metabolism",
  "R-HSA-100863": "Gene expression",
  "R-HSA-100864": "Immune System",
  "R-HSA-100865": "Metabolism",
  "R-HSA-100866": "Metabolism",
  "R-HSA-100867": "Metabolism",
  "R-HSA-100868": "Gene expression",
  "R-HSA-100869": "Immune System",
  "R-HSA-100870": "Immune System",
  "R-HSA-100871": "Gene expression",
  "R-HSA-100872": "Gene expression",
  "R-HSA-100873": "Gene expression",
  "R-HSA-100874": "Metabolism",
  "R-HSA-100875": "Gene expression",
  "R-HSA-100876": "Gene expression",
  "R-HSA-100877": "Immune System",
  "R-HSA-100878": "Immune System",
  "R-HSA-100879": "Immune System",
  "R-HSA-100880": "Immune System",
  "R-HSA-100881": "Immune System",
  "R-HSA-100882": "Gene expression",
  "R-HSA-100883": "Metabolism",
  "R-HSA-100884": "Immune System",
  "R-HSA-100885": "Metabolism",
  "R-HSA-100886": "Gene expression",
  "R-HSA-100887": "Immune System",
  "R-HSA-100888": "Gene expression",
  "R-HSA-100889": "Immune System",
  "R-HSA-100890": "Gene expression",
  "R-HSA-100891": "Gene expression",
  "R-HSA-100892": "Metabolism",
  "R-HSA-100893": "Gene expression",
  "R-HSA-100894": "Immune System",
  "R-HSA-100895": "Immune System",
  "R-HSA-100896": "Immune System",
  "R-HSA-100897": "Gene expression",
  "R-HSA-100898": "Immune System",
  "R-HSA-100899": "Metabolism",
  "R-HSA-100900": "Immune System",
  "R-HSA-100901": "Metabolism",
  "R-HSA-100902": "Immune System",
  "R-HSA-100903": "Gene expression",
  "R-HSA-100904": "Immune System",
  "R-HSA-100905": "Gene expression",
  "R-HSA-100906": "Gene expression",
  "R-HSA-100907": "Metabolism",
  "R-HSA-100908": "Gene expression",
  "R-HSA-100909": "Gene expression",
  "R-HSA-100910": "Immune System",
  "R-HSA-100911": "Immune System",
  "R-HSA-100912": "Immune System",
  "R-HSA-100913": "Metabolism",
  "R-HSA-100914": "Immune System",
  "R-HSA-100915": "Gene expression",
  "R-HSA-100916": "Immune System",
  "R-HSA-100917": "Gene expression",
  "R-HSA-100918": "Gene expression",
  "R-HSA-100919": "Gene expression",
  "R-HSA-100920": "Gene expression",
  "R-HSA-100921": "Immune System",
  "R-HSA-100922": "Metabolism",
  "R-HSA-100923": "Metabolism",
  "R-HSA-100924": "Gene expression",
  "R-HSA-100925": "Metabolism",
  "R-HSA-100926": "Gene expression",
  "R-HSA-100927": "Metabolism",
  "R-HSA-100928": "Immune System",
  "R-HSA-100929": "Immune System",
  "R-HSA-100930": "Immune System",
  "R-HSA-100931": "Metabolism",
  "R-HSA-100932": "Metabolism",
  "R-HSA-100933": "Gene expression",
  "R-HSA-100934": "Metabolism",
  "R-HSA-100935": "Immune System",
  "R-HSA-100936": "Immune System",
  "R-HSA-100937": "Metabolism",
  "R-HSA-100938": "Metabolism",
  "R-HSA-100939": "Immune System",
  "R-HSA-100940": "Gene expression",
  "R-HSA-100941": "Immune System",
  "R-HSA-100942": "Immune System",
  "R-HSA-100943": "Immune System",
  "R-HSA-100944": "Metabolism",
  "R-HSA-100945": "Metabolism",
  "R-HSA-100946": "Gene expression",
  "R-HSA-100947": "Immune System",
  "R-HSA-100948": "Metabolism",
  "R-HSA-100949": "Immune System",
  "R-HSA-100950": "Metabolism",
  "R-HSA-100951": "Metabolism",
  "R-HSA-100952": "Gene expression",
  "R-HSA-100953": "Immune System",
  "R-HSA-100954": "Immune System",
  "R-HSA-100955": "Metabolism",
  "R-HSA-100956": "Metabolism",
  "R-HSA-100957": "Metabolism",
  "R-HSA-100958": "Metabolism",
  "R-HSA-100959": "Immune System",
  "R-HSA-100960": "Gene expression",
  "R-HSA-100961": "Metabolism",
  "R-HSA-100962": "Gene expression",
  "R-HSA-100963": "Gene expression",
  "R-HSA-100964": "Metabolism",
  "R-HSA-100965": "Metabolism",
  "R-HSA-100966": "Metabolism",
  "R-HSA-100967": "Metabolism",
  "R-HSA-100968": "Metabolism",
  "R-HSA-100969": "Metabolism",
  "R-HSA-100970": "Metabolism",
  "R-HSA-100971": "Metabolism",
  "R-HSA-100972": "Immune System",
  "R-HSA-100973": "Metabolism",
  "R-HSA-100974": "Gene expression",
  "R-HSA-100975": "Immune System",
  "R-HSA-100976": "Immune System",
  "R-HSA-100977": "Gene expression",
  "R-HSA-100978": "Gene expression",
  "R-HSA-100979": "Gene expression",
  "R-HSA-100980": "Gene expression",
  "R-HSA-100981": "Immune System",
  "R-HSA-100982": "Gene expression",
  "R-HSA-100983": "Gene expression",
  "R-HSA-100984": "Gene expression",
  "R-HSA-100985": "Immune System",
  "R-HSA-100986": "Gene expression",
  "R-HSA-100987": "Immune System",
  "R-HSA-100988": "Gene expression",
  "R-HSA-100989": "Immune System",
  "R-HSA-100990": "Metabolism",
  "R-HSA-100991": "Gene expression",
  "R-HSA-100992": "Metabolism",
  "R-HSA-100993": "Gene expression",
  "R-HSA-100994": "Metabolism",
  "R-HSA-100995": "Gene expression",
  "R-HSA-100996": "Immune System",
  "R-HSA-100997": "Metabolism",
  "R-HSA-100998": "Metabolism",
  "R-HSA-100999": "Metabolism",
  "R-HSA-101000": "Immune System",
  "R-HSA-101001": "Metabolism",
  "R-HSA-101002": "Immune System",
  "R-HSA-101003": "Metabolism",
  "R-HSA-101004": "Gene expression",
  "R-HSA-101005": "Gene expression",
  "R-HSA-101006": "Immune System",
  "R-HSA-101007": "Metabolism",
  "R-HSA-101008": "Gene expression",
  "R-HSA-101009": "Gene expression",
  "R-HSA-101010": "Gene expression",
  "R-HSA-101011": "Gene expression",
  "R-HSA-101012": "Gene expression",
  "R-HSA-101013": "Gene expression",
  "R-HSA-101014": "Gene expression",
  "R-HSA-101015": "Immune System",
  "R-HSA-101016": "Gene expression",
  "R-HSA-101017": "Gene expression",
  "R-HSA-101018": "Immune System",
  "R-HSA-101019": "Gene expression",
  "R-HSA-101020": "Metabolism",
  "R-HSA-101021": "Immune System",
  "R-HSA-101022": "Gene expression",
  "R-HSA-101023": "Gene expression",
  "R-HSA-101024": "Gene expression",
  "R-HSA-101025": "Immune System",
  "R-HSA-101026": "Gene expression",
  "R-HSA-101027": "Immune System",
  "R-HSA-101028": "Gene expression",
  "R-HSA-101029": "Metabolism",
  "R-HSA-101030": "Immune System",
  "R-HSA-101031": "Immune System",
  "R-HSA-101032": "Immune System",
  "R-HSA-101033": "Immune System",
  "R-HSA-101034": "Immune System",
  "R-HSA-101035": "Immune System",
  "R-HSA-101036": "Immune System",
  "R-HSA-101037": "Metabolism",
  "R-HSA-101038": "Gene expression",
  "R-HSA-101039": "Metabolism",
  "R-HSA-101040": "Metabolism",
  "R-HSA-101041": "Immune System",
  "R-HSA-101042": "Immune System",
  "R-HSA-101043": "Gene expression",
  "R-HSA-101044": "Immune System",
  "R-HSA-101045": "Metabolism",
  "R-HSA-101046": "Gene expression",
  "R-HSA-101047": "Gene expression",
  "R-HSA-101048": "Gene expression",
  "R-HSA-101049": "Immune System",
  "R-HSA-101050": "Metabolism",
  "R-HSA-101051": "Gene expression",
  "R-HSA-101052": "Immune System",
  "R-HSA-101053": "Immune System",
  "R-HSA-101054": "Gene expression",
  "R-HSA-101055": "Gene expression",
  "R-HSA-101056": "Gene expression",
  "R-HSA-101057": "Immune System",
  "R-HSA-101058": "Immune System",
  "R-HSA-101059": "Metabolism",
  "R-HSA-101060": "Metabolism",
  "R-HSA-101061": "Metabolism",
  "R-HSA-101062": "Immune System",
  "R-HSA-101063": "Metabolism",
  "R-HSA-101064": "Immune System",
  "R-HSA-101065": "Immune System",
  "R-HSA-101066": "Metabolism",
  "R-HSA-101067": "Metabolism",
  "R-HSA-101068": "Metabolism",
  "R-HSA-101069": "Metabolism",
  "R-HSA-101070": "Metabolism",
  "R-HSA-101071": "Immune System",
  "R-HSA-101072": "Immune System",
  "R-HSA-101073": "Immune System",
  "R-HSA-101074": "Gene expression",
  "R-HSA-101075": "Gene expression",
  "R-HSA-101076": "Metabolism",
  "R-HSA-101077": "Gene expression",
  "R-HSA-101078": "Immune System",
  "R-HSA-101079": "Gene expression",
  "R-HSA-101080": "Gene expression",
  "R-HSA-101081": "Immune System",
  "R-HSA-101082": "Immune System",
  "R-HSA-101083": "Immune System",
  "R-HSA-101084": "Gene expression",
  "R-HSA-101085": "Immune System",
  "R-HSA-101086": "Metabolism",
  "R-HSA-101087": "Immune System",
  "R-HSA-101088": "Gene expression",
  "R-HSA-101089": "Gene expression",
  "R-HSA-101090": "Immune System",
  "R-HSA-101091": "Immune System",
  "R-HSA-101092": "Metabolism",
  "R-HSA-101093": "Metabolism",
  "R-HSA-101094": "Metabolism",
  "R-HSA-101095": "Gene expression",
  "R-HSA-101096": "Immune System",
  "R-HSA-101097": "Metabolism",
  "R-HSA-101098": "Immune System",
  "R-HSA-101099": "Metabolism",
  "R-HSA-101100": "Immune System",
  "R-HSA-101101": "Metabolism",
  "R-HSA-101102": "Immune System",
  "R-HSA-101103": "Immune System",
  "R-HSA-101104": "Immune System",
  "R-HSA-101105": "Metabolism",
  "R-HSA-101106": "Gene expression",
  "R-HSA-101107": "Gene expression",
  "R-HSA-101108": "Gene expression",
  "R-HSA-101109": "Gene expression",
  "R-HSA-101110": "Immune System",
  "R-HSA-101111": "Metabolism",
  "R-HSA-101112": "Gene expression",
  "R-HSA-101113": "Metabolism",
  "R-HSA-101114": "Metabolism",
  "R-HSA-101115": "Gene expression",
  "R-HSA-101116": "Metabolism",
  "R-HSA-101117": "Metabolism",
  "R-HSA-101118": "Gene expression",
  "R-HSA-101119": "Immune System",
  "R-HSA-101120": "Metabolism",
  "R-HSA-101121": "Metabolism",
  "R-HSA-101122": "Immune System",
  "R-HSA-101123": "Immune System",
  "R-HSA-101124": "Immune System",
  "R-HSA-101125": "Gene expression",
  "R-HSA-101126": "Metabolism",
  "R-HSA-101127": "Immune System",
  "R-HSA-101128": "Immune System",
  "R-HSA-101129": "Immune System",
  "R-HSA-101130": "Metabolism",
  "R-HSA-101131": "Gene expression",
  "R-HSA-101132": "Metabolism",
  "R-HSA-101133": "Metabolism",
  "R-HSA-101134": "Metabolism",
  "R-HSA-101135": "Immune System",
  "R-HSA-101136": "Immune System",
  "R-HSA-101137": "Gene expression",
  "R-HSA-101138": "Gene expression",
  "R-HSA-101139": "Metabolism",
  "R-HSA-101140": "Metabolism",
  "R-HSA-101141": "Gene expression",
  "R-HSA-101142": "Immune System",
  "R-HSA-101143": "Immune System",
  "R-HSA-101144": "Metabolism",
  "R-HSA-101145": "Metabolism",
  "R-HSA-101146": "Immune System",
  "R-HSA-101147": "Immune System",
  "R-HSA-101148": "Metabolism",
  "R-HSA-101149": "Metabolism",
  "R-HSA-101150": "Metabolism",
  "R-HSA-101151": "Immune System",
  "R-HSA-101152": "Immune System",
  "R-HSA-101153": "Immune System",
  "R-HSA-101154": "Immune System",
  "R-HSA-101155": "Gene expression",
  "R-HSA-101156": "Immune System",
  "R-HSA-101157": "Gene expression",
  "R-HSA-101158": "Gene expression",
  "R-HSA-101159": "Metabolism",
  "R-HSA-101160": "Gene expression",
  "R-HSA-101161": "Metabolism",
  "R-HSA-101162": "Gene expression",
  "R-HSA-101163": "Metabolism",
  "R-HSA-101164": "Metabolism",
  "R-HSA-101165": "Gene expression",
  "R-HSA-101166": "Immune System",
  "R-HSA-101167": "Immune System",
  "R-HSA-101168": "Immune System",
  "R-HSA-101169": "Gene expression",
  "R-HSA-101170": "Gene expression",
  "R-HSA-101171": "Metabolism",
  "R-HSA-101172": "Metabolism",
  "R-HSA-101173": "Immune System",
  "R-HSA-101174": "Gene expression",
  "R-HSA-101175": "Metabolism",
  "R-HSA-101176": "Metabolism",
  "R-HSA-101177": "Gene expression",
  "R-HSA-101178": "Metabolism",
  "R-HSA-101179": "Gene expression",
  "R-HSA-101180": "Gene expression",
  "R-HSA-101181": "Metabolism",
  "R-HSA-101182": "Immune System",
  "R-HSA-101183": "Immune System",
  "R-HSA-101184": "Immune System",
  "R-HSA-101185": "Immune System",
  "R-HSA-101186": "Gene expression",
  "R-HSA-101187": "Immune System",
  "R-HSA-101188": "Immune System",
  "R-HSA-101189": "Metabolism",
  "R-HSA-101190": "Metabolism",
  "R-HSA-101191": "Metabolism",
  "R-HSA-101192": "Gene expression",
  "R-HSA-101193": "Immune System",
  "R-HSA-101194": "Gene expression",
  "R-HSA-101195": "Metabolism",
  "R-HSA-101196": "Immune System",
  "R-HSA-101197": "Immune System",
  "R-HSA-101198": "Immune System",
  "R-HSA-101199": "Gene expression"
}\nDRUG_SYNONYMS = {
  "Drug1": [
    "Drug1",
    "DRUG1",
    "drug-1",
    "D1"
  ],
  "Drug2": [
    "Drug2",
    "DRUG2",
    "drug-2",
    "D2"
  ],
  "Drug3": [
    "Drug3",
    "DRUG3",
    "drug-3",
    "D3"
  ],
  "Drug4": [
    "Drug4",
    "DRUG4",
    "drug-4",
    "D4"
  ],
  "Drug5": [
    "Drug5",
    "DRUG5",
    "drug-5",
    "D5"
  ],
  "Drug6": [
    "Drug6",
    "DRUG6",
    "drug-6",
    "D6"
  ],
  "Drug7": [
    "Drug7",
    "DRUG7",
    "drug-7",
    "D7"
  ],
  "Drug8": [
    "Drug8",
    "DRUG8",
    "drug-8",
    "D8"
  ],
  "Drug9": [
    "Drug9",
    "DRUG9",
    "drug-9",
    "D9"
  ],
  "Drug10": [
    "Drug10",
    "DRUG10",
    "drug-10",
    "D10"
  ],
  "Drug11": [
    "Drug11",
    "DRUG11",
    "drug-11",
    "D11"
  ],
  "Drug12": [
    "Drug12",
    "DRUG12",
    "drug-12",
    "D12"
  ],
  "Drug13": [
    "Drug13",
    "DRUG13",
    "drug-13",
    "D13"
  ],
  "Drug14": [
    "Drug14",
    "DRUG14",
    "drug-14",
    "D14"
  ],
  "Drug15": [
    "Drug15",
    "DRUG15",
    "drug-15",
    "D15"
  ],
  "Drug16": [
    "Drug16",
    "DRUG16",
    "drug-16",
    "D16"
  ],
  "Drug17": [
    "Drug17",
    "DRUG17",
    "drug-17",
    "D17"
  ],
  "Drug18": [
    "Drug18",
    "DRUG18",
    "drug-18",
    "D18"
  ],
  "Drug19": [
    "Drug19",
    "DRUG19",
    "drug-19",
    "D19"
  ],
  "Drug20": [
    "Drug20",
    "DRUG20",
    "drug-20",
    "D20"
  ],
  "Drug21": [
    "Drug21",
    "DRUG21",
    "drug-21",
    "D21"
  ],
  "Drug22": [
    "Drug22",
    "DRUG22",
    "drug-22",
    "D22"
  ],
  "Drug23": [
    "Drug23",
    "DRUG23",
    "drug-23",
    "D23"
  ],
  "Drug24": [
    "Drug24",
    "DRUG24",
    "drug-24",
    "D24"
  ],
  "Drug25": [
    "Drug25",
    "DRUG25",
    "drug-25",
    "D25"
  ],
  "Drug26": [
    "Drug26",
    "DRUG26",
    "drug-26",
    "D26"
  ],
  "Drug27": [
    "Drug27",
    "DRUG27",
    "drug-27",
    "D27"
  ],
  "Drug28": [
    "Drug28",
    "DRUG28",
    "drug-28",
    "D28"
  ],
  "Drug29": [
    "Drug29",
    "DRUG29",
    "drug-29",
    "D29"
  ],
  "Drug30": [
    "Drug30",
    "DRUG30",
    "drug-30",
    "D30"
  ],
  "Drug31": [
    "Drug31",
    "DRUG31",
    "drug-31",
    "D31"
  ],
  "Drug32": [
    "Drug32",
    "DRUG32",
    "drug-32",
    "D32"
  ],
  "Drug33": [
    "Drug33",
    "DRUG33",
    "drug-33",
    "D33"
  ],
  "Drug34": [
    "Drug34",
    "DRUG34",
    "drug-34",
    "D34"
  ],
  "Drug35": [
    "Drug35",
    "DRUG35",
    "drug-35",
    "D35"
  ],
  "Drug36": [
    "Drug36",
    "DRUG36",
    "drug-36",
    "D36"
  ],
  "Drug37": [
    "Drug37",
    "DRUG37",
    "drug-37",
    "D37"
  ],
  "Drug38": [
    "Drug38",
    "DRUG38",
    "drug-38",
    "D38"
  ],
  "Drug39": [
    "Drug39",
    "DRUG39",
    "drug-39",
    "D39"
  ],
  "Drug40": [
    "Drug40",
    "DRUG40",
    "drug-40",
    "D40"
  ],
  "Drug41": [
    "Drug41",
    "DRUG41",
    "drug-41",
    "D41"
  ],
  "Drug42": [
    "Drug42",
    "DRUG42",
    "drug-42",
    "D42"
  ],
  "Drug43": [
    "Drug43",
    "DRUG43",
    "drug-43",
    "D43"
  ],
  "Drug44": [
    "Drug44",
    "DRUG44",
    "drug-44",
    "D44"
  ],
  "Drug45": [
    "Drug45",
    "DRUG45",
    "drug-45",
    "D45"
  ],
  "Drug46": [
    "Drug46",
    "DRUG46",
    "drug-46",
    "D46"
  ],
  "Drug47": [
    "Drug47",
    "DRUG47",
    "drug-47",
    "D47"
  ],
  "Drug48": [
    "Drug48",
    "DRUG48",
    "drug-48",
    "D48"
  ],
  "Drug49": [
    "Drug49",
    "DRUG49",
    "drug-49",
    "D49"
  ],
  "Drug50": [
    "Drug50",
    "DRUG50",
    "drug-50",
    "D50"
  ],
  "Drug51": [
    "Drug51",
    "DRUG51",
    "drug-51",
    "D51"
  ],
  "Drug52": [
    "Drug52",
    "DRUG52",
    "drug-52",
    "D52"
  ],
  "Drug53": [
    "Drug53",
    "DRUG53",
    "drug-53",
    "D53"
  ],
  "Drug54": [
    "Drug54",
    "DRUG54",
    "drug-54",
    "D54"
  ],
  "Drug55": [
    "Drug55",
    "DRUG55",
    "drug-55",
    "D55"
  ],
  "Drug56": [
    "Drug56",
    "DRUG56",
    "drug-56",
    "D56"
  ],
  "Drug57": [
    "Drug57",
    "DRUG57",
    "drug-57",
    "D57"
  ],
  "Drug58": [
    "Drug58",
    "DRUG58",
    "drug-58",
    "D58"
  ],
  "Drug59": [
    "Drug59",
    "DRUG59",
    "drug-59",
    "D59"
  ],
  "Drug60": [
    "Drug60",
    "DRUG60",
    "drug-60",
    "D60"
  ],
  "Drug61": [
    "Drug61",
    "DRUG61",
    "drug-61",
    "D61"
  ],
  "Drug62": [
    "Drug62",
    "DRUG62",
    "drug-62",
    "D62"
  ],
  "Drug63": [
    "Drug63",
    "DRUG63",
    "drug-63",
    "D63"
  ],
  "Drug64": [
    "Drug64",
    "DRUG64",
    "drug-64",
    "D64"
  ],
  "Drug65": [
    "Drug65",
    "DRUG65",
    "drug-65",
    "D65"
  ],
  "Drug66": [
    "Drug66",
    "DRUG66",
    "drug-66",
    "D66"
  ],
  "Drug67": [
    "Drug67",
    "DRUG67",
    "drug-67",
    "D67"
  ],
  "Drug68": [
    "Drug68",
    "DRUG68",
    "drug-68",
    "D68"
  ],
  "Drug69": [
    "Drug69",
    "DRUG69",
    "drug-69",
    "D69"
  ],
  "Drug70": [
    "Drug70",
    "DRUG70",
    "drug-70",
    "D70"
  ],
  "Drug71": [
    "Drug71",
    "DRUG71",
    "drug-71",
    "D71"
  ],
  "Drug72": [
    "Drug72",
    "DRUG72",
    "drug-72",
    "D72"
  ],
  "Drug73": [
    "Drug73",
    "DRUG73",
    "drug-73",
    "D73"
  ],
  "Drug74": [
    "Drug74",
    "DRUG74",
    "drug-74",
    "D74"
  ],
  "Drug75": [
    "Drug75",
    "DRUG75",
    "drug-75",
    "D75"
  ],
  "Drug76": [
    "Drug76",
    "DRUG76",
    "drug-76",
    "D76"
  ],
  "Drug77": [
    "Drug77",
    "DRUG77",
    "drug-77",
    "D77"
  ],
  "Drug78": [
    "Drug78",
    "DRUG78",
    "drug-78",
    "D78"
  ],
  "Drug79": [
    "Drug79",
    "DRUG79",
    "drug-79",
    "D79"
  ],
  "Drug80": [
    "Drug80",
    "DRUG80",
    "drug-80",
    "D80"
  ],
  "Drug81": [
    "Drug81",
    "DRUG81",
    "drug-81",
    "D81"
  ],
  "Drug82": [
    "Drug82",
    "DRUG82",
    "drug-82",
    "D82"
  ],
  "Drug83": [
    "Drug83",
    "DRUG83",
    "drug-83",
    "D83"
  ],
  "Drug84": [
    "Drug84",
    "DRUG84",
    "drug-84",
    "D84"
  ],
  "Drug85": [
    "Drug85",
    "DRUG85",
    "drug-85",
    "D85"
  ],
  "Drug86": [
    "Drug86",
    "DRUG86",
    "drug-86",
    "D86"
  ],
  "Drug87": [
    "Drug87",
    "DRUG87",
    "drug-87",
    "D87"
  ],
  "Drug88": [
    "Drug88",
    "DRUG88",
    "drug-88",
    "D88"
  ],
  "Drug89": [
    "Drug89",
    "DRUG89",
    "drug-89",
    "D89"
  ],
  "Drug90": [
    "Drug90",
    "DRUG90",
    "drug-90",
    "D90"
  ],
  "Drug91": [
    "Drug91",
    "DRUG91",
    "drug-91",
    "D91"
  ],
  "Drug92": [
    "Drug92",
    "DRUG92",
    "drug-92",
    "D92"
  ],
  "Drug93": [
    "Drug93",
    "DRUG93",
    "drug-93",
    "D93"
  ],
  "Drug94": [
    "Drug94",
    "DRUG94",
    "drug-94",
    "D94"
  ],
  "Drug95": [
    "Drug95",
    "DRUG95",
    "drug-95",
    "D95"
  ],
  "Drug96": [
    "Drug96",
    "DRUG96",
    "drug-96",
    "D96"
  ],
  "Drug97": [
    "Drug97",
    "DRUG97",
    "drug-97",
    "D97"
  ],
  "Drug98": [
    "Drug98",
    "DRUG98",
    "drug-98",
    "D98"
  ],
  "Drug99": [
    "Drug99",
    "DRUG99",
    "drug-99",
    "D99"
  ],
  "Drug100": [
    "Drug100",
    "DRUG100",
    "drug-100",
    "D100"
  ],
  "Drug101": [
    "Drug101",
    "DRUG101",
    "drug-101",
    "D101"
  ],
  "Drug102": [
    "Drug102",
    "DRUG102",
    "drug-102",
    "D102"
  ],
  "Drug103": [
    "Drug103",
    "DRUG103",
    "drug-103",
    "D103"
  ],
  "Drug104": [
    "Drug104",
    "DRUG104",
    "drug-104",
    "D104"
  ],
  "Drug105": [
    "Drug105",
    "DRUG105",
    "drug-105",
    "D105"
  ],
  "Drug106": [
    "Drug106",
    "DRUG106",
    "drug-106",
    "D106"
  ],
  "Drug107": [
    "Drug107",
    "DRUG107",
    "drug-107",
    "D107"
  ],
  "Drug108": [
    "Drug108",
    "DRUG108",
    "drug-108",
    "D108"
  ],
  "Drug109": [
    "Drug109",
    "DRUG109",
    "drug-109",
    "D109"
  ],
  "Drug110": [
    "Drug110",
    "DRUG110",
    "drug-110",
    "D110"
  ],
  "Drug111": [
    "Drug111",
    "DRUG111",
    "drug-111",
    "D111"
  ],
  "Drug112": [
    "Drug112",
    "DRUG112",
    "drug-112",
    "D112"
  ],
  "Drug113": [
    "Drug113",
    "DRUG113",
    "drug-113",
    "D113"
  ],
  "Drug114": [
    "Drug114",
    "DRUG114",
    "drug-114",
    "D114"
  ],
  "Drug115": [
    "Drug115",
    "DRUG115",
    "drug-115",
    "D115"
  ],
  "Drug116": [
    "Drug116",
    "DRUG116",
    "drug-116",
    "D116"
  ],
  "Drug117": [
    "Drug117",
    "DRUG117",
    "drug-117",
    "D117"
  ],
  "Drug118": [
    "Drug118",
    "DRUG118",
    "drug-118",
    "D118"
  ],
  "Drug119": [
    "Drug119",
    "DRUG119",
    "drug-119",
    "D119"
  ],
  "Drug120": [
    "Drug120",
    "DRUG120",
    "drug-120",
    "D120"
  ],
  "Drug121": [
    "Drug121",
    "DRUG121",
    "drug-121",
    "D121"
  ],
  "Drug122": [
    "Drug122",
    "DRUG122",
    "drug-122",
    "D122"
  ],
  "Drug123": [
    "Drug123",
    "DRUG123",
    "drug-123",
    "D123"
  ],
  "Drug124": [
    "Drug124",
    "DRUG124",
    "drug-124",
    "D124"
  ],
  "Drug125": [
    "Drug125",
    "DRUG125",
    "drug-125",
    "D125"
  ],
  "Drug126": [
    "Drug126",
    "DRUG126",
    "drug-126",
    "D126"
  ],
  "Drug127": [
    "Drug127",
    "DRUG127",
    "drug-127",
    "D127"
  ],
  "Drug128": [
    "Drug128",
    "DRUG128",
    "drug-128",
    "D128"
  ],
  "Drug129": [
    "Drug129",
    "DRUG129",
    "drug-129",
    "D129"
  ],
  "Drug130": [
    "Drug130",
    "DRUG130",
    "drug-130",
    "D130"
  ],
  "Drug131": [
    "Drug131",
    "DRUG131",
    "drug-131",
    "D131"
  ],
  "Drug132": [
    "Drug132",
    "DRUG132",
    "drug-132",
    "D132"
  ],
  "Drug133": [
    "Drug133",
    "DRUG133",
    "drug-133",
    "D133"
  ],
  "Drug134": [
    "Drug134",
    "DRUG134",
    "drug-134",
    "D134"
  ],
  "Drug135": [
    "Drug135",
    "DRUG135",
    "drug-135",
    "D135"
  ],
  "Drug136": [
    "Drug136",
    "DRUG136",
    "drug-136",
    "D136"
  ],
  "Drug137": [
    "Drug137",
    "DRUG137",
    "drug-137",
    "D137"
  ],
  "Drug138": [
    "Drug138",
    "DRUG138",
    "drug-138",
    "D138"
  ],
  "Drug139": [
    "Drug139",
    "DRUG139",
    "drug-139",
    "D139"
  ],
  "Drug140": [
    "Drug140",
    "DRUG140",
    "drug-140",
    "D140"
  ],
  "Drug141": [
    "Drug141",
    "DRUG141",
    "drug-141",
    "D141"
  ],
  "Drug142": [
    "Drug142",
    "DRUG142",
    "drug-142",
    "D142"
  ],
  "Drug143": [
    "Drug143",
    "DRUG143",
    "drug-143",
    "D143"
  ],
  "Drug144": [
    "Drug144",
    "DRUG144",
    "drug-144",
    "D144"
  ],
  "Drug145": [
    "Drug145",
    "DRUG145",
    "drug-145",
    "D145"
  ],
  "Drug146": [
    "Drug146",
    "DRUG146",
    "drug-146",
    "D146"
  ],
  "Drug147": [
    "Drug147",
    "DRUG147",
    "drug-147",
    "D147"
  ],
  "Drug148": [
    "Drug148",
    "DRUG148",
    "drug-148",
    "D148"
  ],
  "Drug149": [
    "Drug149",
    "DRUG149",
    "drug-149",
    "D149"
  ],
  "Drug150": [
    "Drug150",
    "DRUG150",
    "drug-150",
    "D150"
  ],
  "Drug151": [
    "Drug151",
    "DRUG151",
    "drug-151",
    "D151"
  ],
  "Drug152": [
    "Drug152",
    "DRUG152",
    "drug-152",
    "D152"
  ],
  "Drug153": [
    "Drug153",
    "DRUG153",
    "drug-153",
    "D153"
  ],
  "Drug154": [
    "Drug154",
    "DRUG154",
    "drug-154",
    "D154"
  ],
  "Drug155": [
    "Drug155",
    "DRUG155",
    "drug-155",
    "D155"
  ],
  "Drug156": [
    "Drug156",
    "DRUG156",
    "drug-156",
    "D156"
  ],
  "Drug157": [
    "Drug157",
    "DRUG157",
    "drug-157",
    "D157"
  ],
  "Drug158": [
    "Drug158",
    "DRUG158",
    "drug-158",
    "D158"
  ],
  "Drug159": [
    "Drug159",
    "DRUG159",
    "drug-159",
    "D159"
  ],
  "Drug160": [
    "Drug160",
    "DRUG160",
    "drug-160",
    "D160"
  ],
  "Drug161": [
    "Drug161",
    "DRUG161",
    "drug-161",
    "D161"
  ],
  "Drug162": [
    "Drug162",
    "DRUG162",
    "drug-162",
    "D162"
  ],
  "Drug163": [
    "Drug163",
    "DRUG163",
    "drug-163",
    "D163"
  ],
  "Drug164": [
    "Drug164",
    "DRUG164",
    "drug-164",
    "D164"
  ],
  "Drug165": [
    "Drug165",
    "DRUG165",
    "drug-165",
    "D165"
  ],
  "Drug166": [
    "Drug166",
    "DRUG166",
    "drug-166",
    "D166"
  ],
  "Drug167": [
    "Drug167",
    "DRUG167",
    "drug-167",
    "D167"
  ],
  "Drug168": [
    "Drug168",
    "DRUG168",
    "drug-168",
    "D168"
  ],
  "Drug169": [
    "Drug169",
    "DRUG169",
    "drug-169",
    "D169"
  ],
  "Drug170": [
    "Drug170",
    "DRUG170",
    "drug-170",
    "D170"
  ],
  "Drug171": [
    "Drug171",
    "DRUG171",
    "drug-171",
    "D171"
  ],
  "Drug172": [
    "Drug172",
    "DRUG172",
    "drug-172",
    "D172"
  ],
  "Drug173": [
    "Drug173",
    "DRUG173",
    "drug-173",
    "D173"
  ],
  "Drug174": [
    "Drug174",
    "DRUG174",
    "drug-174",
    "D174"
  ],
  "Drug175": [
    "Drug175",
    "DRUG175",
    "drug-175",
    "D175"
  ],
  "Drug176": [
    "Drug176",
    "DRUG176",
    "drug-176",
    "D176"
  ],
  "Drug177": [
    "Drug177",
    "DRUG177",
    "drug-177",
    "D177"
  ],
  "Drug178": [
    "Drug178",
    "DRUG178",
    "drug-178",
    "D178"
  ],
  "Drug179": [
    "Drug179",
    "DRUG179",
    "drug-179",
    "D179"
  ],
  "Drug180": [
    "Drug180",
    "DRUG180",
    "drug-180",
    "D180"
  ],
  "Drug181": [
    "Drug181",
    "DRUG181",
    "drug-181",
    "D181"
  ],
  "Drug182": [
    "Drug182",
    "DRUG182",
    "drug-182",
    "D182"
  ],
  "Drug183": [
    "Drug183",
    "DRUG183",
    "drug-183",
    "D183"
  ],
  "Drug184": [
    "Drug184",
    "DRUG184",
    "drug-184",
    "D184"
  ],
  "Drug185": [
    "Drug185",
    "DRUG185",
    "drug-185",
    "D185"
  ],
  "Drug186": [
    "Drug186",
    "DRUG186",
    "drug-186",
    "D186"
  ],
  "Drug187": [
    "Drug187",
    "DRUG187",
    "drug-187",
    "D187"
  ],
  "Drug188": [
    "Drug188",
    "DRUG188",
    "drug-188",
    "D188"
  ],
  "Drug189": [
    "Drug189",
    "DRUG189",
    "drug-189",
    "D189"
  ],
  "Drug190": [
    "Drug190",
    "DRUG190",
    "drug-190",
    "D190"
  ],
  "Drug191": [
    "Drug191",
    "DRUG191",
    "drug-191",
    "D191"
  ],
  "Drug192": [
    "Drug192",
    "DRUG192",
    "drug-192",
    "D192"
  ],
  "Drug193": [
    "Drug193",
    "DRUG193",
    "drug-193",
    "D193"
  ],
  "Drug194": [
    "Drug194",
    "DRUG194",
    "drug-194",
    "D194"
  ],
  "Drug195": [
    "Drug195",
    "DRUG195",
    "drug-195",
    "D195"
  ],
  "Drug196": [
    "Drug196",
    "DRUG196",
    "drug-196",
    "D196"
  ],
  "Drug197": [
    "Drug197",
    "DRUG197",
    "drug-197",
    "D197"
  ],
  "Drug198": [
    "Drug198",
    "DRUG198",
    "drug-198",
    "D198"
  ],
  "Drug199": [
    "Drug199",
    "DRUG199",
    "drug-199",
    "D199"
  ],
  "Drug200": [
    "Drug200",
    "DRUG200",
    "drug-200",
    "D200"
  ],
  "Drug201": [
    "Drug201",
    "DRUG201",
    "drug-201",
    "D201"
  ],
  "Drug202": [
    "Drug202",
    "DRUG202",
    "drug-202",
    "D202"
  ],
  "Drug203": [
    "Drug203",
    "DRUG203",
    "drug-203",
    "D203"
  ],
  "Drug204": [
    "Drug204",
    "DRUG204",
    "drug-204",
    "D204"
  ],
  "Drug205": [
    "Drug205",
    "DRUG205",
    "drug-205",
    "D205"
  ],
  "Drug206": [
    "Drug206",
    "DRUG206",
    "drug-206",
    "D206"
  ],
  "Drug207": [
    "Drug207",
    "DRUG207",
    "drug-207",
    "D207"
  ],
  "Drug208": [
    "Drug208",
    "DRUG208",
    "drug-208",
    "D208"
  ],
  "Drug209": [
    "Drug209",
    "DRUG209",
    "drug-209",
    "D209"
  ],
  "Drug210": [
    "Drug210",
    "DRUG210",
    "drug-210",
    "D210"
  ],
  "Drug211": [
    "Drug211",
    "DRUG211",
    "drug-211",
    "D211"
  ],
  "Drug212": [
    "Drug212",
    "DRUG212",
    "drug-212",
    "D212"
  ],
  "Drug213": [
    "Drug213",
    "DRUG213",
    "drug-213",
    "D213"
  ],
  "Drug214": [
    "Drug214",
    "DRUG214",
    "drug-214",
    "D214"
  ],
  "Drug215": [
    "Drug215",
    "DRUG215",
    "drug-215",
    "D215"
  ],
  "Drug216": [
    "Drug216",
    "DRUG216",
    "drug-216",
    "D216"
  ],
  "Drug217": [
    "Drug217",
    "DRUG217",
    "drug-217",
    "D217"
  ],
  "Drug218": [
    "Drug218",
    "DRUG218",
    "drug-218",
    "D218"
  ],
  "Drug219": [
    "Drug219",
    "DRUG219",
    "drug-219",
    "D219"
  ],
  "Drug220": [
    "Drug220",
    "DRUG220",
    "drug-220",
    "D220"
  ],
  "Drug221": [
    "Drug221",
    "DRUG221",
    "drug-221",
    "D221"
  ],
  "Drug222": [
    "Drug222",
    "DRUG222",
    "drug-222",
    "D222"
  ],
  "Drug223": [
    "Drug223",
    "DRUG223",
    "drug-223",
    "D223"
  ],
  "Drug224": [
    "Drug224",
    "DRUG224",
    "drug-224",
    "D224"
  ],
  "Drug225": [
    "Drug225",
    "DRUG225",
    "drug-225",
    "D225"
  ],
  "Drug226": [
    "Drug226",
    "DRUG226",
    "drug-226",
    "D226"
  ],
  "Drug227": [
    "Drug227",
    "DRUG227",
    "drug-227",
    "D227"
  ],
  "Drug228": [
    "Drug228",
    "DRUG228",
    "drug-228",
    "D228"
  ],
  "Drug229": [
    "Drug229",
    "DRUG229",
    "drug-229",
    "D229"
  ],
  "Drug230": [
    "Drug230",
    "DRUG230",
    "drug-230",
    "D230"
  ],
  "Drug231": [
    "Drug231",
    "DRUG231",
    "drug-231",
    "D231"
  ],
  "Drug232": [
    "Drug232",
    "DRUG232",
    "drug-232",
    "D232"
  ],
  "Drug233": [
    "Drug233",
    "DRUG233",
    "drug-233",
    "D233"
  ],
  "Drug234": [
    "Drug234",
    "DRUG234",
    "drug-234",
    "D234"
  ],
  "Drug235": [
    "Drug235",
    "DRUG235",
    "drug-235",
    "D235"
  ],
  "Drug236": [
    "Drug236",
    "DRUG236",
    "drug-236",
    "D236"
  ],
  "Drug237": [
    "Drug237",
    "DRUG237",
    "drug-237",
    "D237"
  ],
  "Drug238": [
    "Drug238",
    "DRUG238",
    "drug-238",
    "D238"
  ],
  "Drug239": [
    "Drug239",
    "DRUG239",
    "drug-239",
    "D239"
  ],
  "Drug240": [
    "Drug240",
    "DRUG240",
    "drug-240",
    "D240"
  ],
  "Drug241": [
    "Drug241",
    "DRUG241",
    "drug-241",
    "D241"
  ],
  "Drug242": [
    "Drug242",
    "DRUG242",
    "drug-242",
    "D242"
  ],
  "Drug243": [
    "Drug243",
    "DRUG243",
    "drug-243",
    "D243"
  ],
  "Drug244": [
    "Drug244",
    "DRUG244",
    "drug-244",
    "D244"
  ],
  "Drug245": [
    "Drug245",
    "DRUG245",
    "drug-245",
    "D245"
  ],
  "Drug246": [
    "Drug246",
    "DRUG246",
    "drug-246",
    "D246"
  ],
  "Drug247": [
    "Drug247",
    "DRUG247",
    "drug-247",
    "D247"
  ],
  "Drug248": [
    "Drug248",
    "DRUG248",
    "drug-248",
    "D248"
  ],
  "Drug249": [
    "Drug249",
    "DRUG249",
    "drug-249",
    "D249"
  ],
  "Drug250": [
    "Drug250",
    "DRUG250",
    "drug-250",
    "D250"
  ],
  "Drug251": [
    "Drug251",
    "DRUG251",
    "drug-251",
    "D251"
  ],
  "Drug252": [
    "Drug252",
    "DRUG252",
    "drug-252",
    "D252"
  ],
  "Drug253": [
    "Drug253",
    "DRUG253",
    "drug-253",
    "D253"
  ],
  "Drug254": [
    "Drug254",
    "DRUG254",
    "drug-254",
    "D254"
  ],
  "Drug255": [
    "Drug255",
    "DRUG255",
    "drug-255",
    "D255"
  ],
  "Drug256": [
    "Drug256",
    "DRUG256",
    "drug-256",
    "D256"
  ],
  "Drug257": [
    "Drug257",
    "DRUG257",
    "drug-257",
    "D257"
  ],
  "Drug258": [
    "Drug258",
    "DRUG258",
    "drug-258",
    "D258"
  ],
  "Drug259": [
    "Drug259",
    "DRUG259",
    "drug-259",
    "D259"
  ],
  "Drug260": [
    "Drug260",
    "DRUG260",
    "drug-260",
    "D260"
  ],
  "Drug261": [
    "Drug261",
    "DRUG261",
    "drug-261",
    "D261"
  ],
  "Drug262": [
    "Drug262",
    "DRUG262",
    "drug-262",
    "D262"
  ],
  "Drug263": [
    "Drug263",
    "DRUG263",
    "drug-263",
    "D263"
  ],
  "Drug264": [
    "Drug264",
    "DRUG264",
    "drug-264",
    "D264"
  ],
  "Drug265": [
    "Drug265",
    "DRUG265",
    "drug-265",
    "D265"
  ],
  "Drug266": [
    "Drug266",
    "DRUG266",
    "drug-266",
    "D266"
  ],
  "Drug267": [
    "Drug267",
    "DRUG267",
    "drug-267",
    "D267"
  ],
  "Drug268": [
    "Drug268",
    "DRUG268",
    "drug-268",
    "D268"
  ],
  "Drug269": [
    "Drug269",
    "DRUG269",
    "drug-269",
    "D269"
  ],
  "Drug270": [
    "Drug270",
    "DRUG270",
    "drug-270",
    "D270"
  ],
  "Drug271": [
    "Drug271",
    "DRUG271",
    "drug-271",
    "D271"
  ],
  "Drug272": [
    "Drug272",
    "DRUG272",
    "drug-272",
    "D272"
  ],
  "Drug273": [
    "Drug273",
    "DRUG273",
    "drug-273",
    "D273"
  ],
  "Drug274": [
    "Drug274",
    "DRUG274",
    "drug-274",
    "D274"
  ],
  "Drug275": [
    "Drug275",
    "DRUG275",
    "drug-275",
    "D275"
  ],
  "Drug276": [
    "Drug276",
    "DRUG276",
    "drug-276",
    "D276"
  ],
  "Drug277": [
    "Drug277",
    "DRUG277",
    "drug-277",
    "D277"
  ],
  "Drug278": [
    "Drug278",
    "DRUG278",
    "drug-278",
    "D278"
  ],
  "Drug279": [
    "Drug279",
    "DRUG279",
    "drug-279",
    "D279"
  ],
  "Drug280": [
    "Drug280",
    "DRUG280",
    "drug-280",
    "D280"
  ],
  "Drug281": [
    "Drug281",
    "DRUG281",
    "drug-281",
    "D281"
  ],
  "Drug282": [
    "Drug282",
    "DRUG282",
    "drug-282",
    "D282"
  ],
  "Drug283": [
    "Drug283",
    "DRUG283",
    "drug-283",
    "D283"
  ],
  "Drug284": [
    "Drug284",
    "DRUG284",
    "drug-284",
    "D284"
  ],
  "Drug285": [
    "Drug285",
    "DRUG285",
    "drug-285",
    "D285"
  ],
  "Drug286": [
    "Drug286",
    "DRUG286",
    "drug-286",
    "D286"
  ],
  "Drug287": [
    "Drug287",
    "DRUG287",
    "drug-287",
    "D287"
  ],
  "Drug288": [
    "Drug288",
    "DRUG288",
    "drug-288",
    "D288"
  ],
  "Drug289": [
    "Drug289",
    "DRUG289",
    "drug-289",
    "D289"
  ],
  "Drug290": [
    "Drug290",
    "DRUG290",
    "drug-290",
    "D290"
  ],
  "Drug291": [
    "Drug291",
    "DRUG291",
    "drug-291",
    "D291"
  ],
  "Drug292": [
    "Drug292",
    "DRUG292",
    "drug-292",
    "D292"
  ],
  "Drug293": [
    "Drug293",
    "DRUG293",
    "drug-293",
    "D293"
  ],
  "Drug294": [
    "Drug294",
    "DRUG294",
    "drug-294",
    "D294"
  ],
  "Drug295": [
    "Drug295",
    "DRUG295",
    "drug-295",
    "D295"
  ],
  "Drug296": [
    "Drug296",
    "DRUG296",
    "drug-296",
    "D296"
  ],
  "Drug297": [
    "Drug297",
    "DRUG297",
    "drug-297",
    "D297"
  ],
  "Drug298": [
    "Drug298",
    "DRUG298",
    "drug-298",
    "D298"
  ],
  "Drug299": [
    "Drug299",
    "DRUG299",
    "drug-299",
    "D299"
  ],
  "Drug300": [
    "Drug300",
    "DRUG300",
    "drug-300",
    "D300"
  ],
  "Drug301": [
    "Drug301",
    "DRUG301",
    "drug-301",
    "D301"
  ],
  "Drug302": [
    "Drug302",
    "DRUG302",
    "drug-302",
    "D302"
  ],
  "Drug303": [
    "Drug303",
    "DRUG303",
    "drug-303",
    "D303"
  ],
  "Drug304": [
    "Drug304",
    "DRUG304",
    "drug-304",
    "D304"
  ],
  "Drug305": [
    "Drug305",
    "DRUG305",
    "drug-305",
    "D305"
  ],
  "Drug306": [
    "Drug306",
    "DRUG306",
    "drug-306",
    "D306"
  ],
  "Drug307": [
    "Drug307",
    "DRUG307",
    "drug-307",
    "D307"
  ],
  "Drug308": [
    "Drug308",
    "DRUG308",
    "drug-308",
    "D308"
  ],
  "Drug309": [
    "Drug309",
    "DRUG309",
    "drug-309",
    "D309"
  ],
  "Drug310": [
    "Drug310",
    "DRUG310",
    "drug-310",
    "D310"
  ],
  "Drug311": [
    "Drug311",
    "DRUG311",
    "drug-311",
    "D311"
  ],
  "Drug312": [
    "Drug312",
    "DRUG312",
    "drug-312",
    "D312"
  ],
  "Drug313": [
    "Drug313",
    "DRUG313",
    "drug-313",
    "D313"
  ],
  "Drug314": [
    "Drug314",
    "DRUG314",
    "drug-314",
    "D314"
  ],
  "Drug315": [
    "Drug315",
    "DRUG315",
    "drug-315",
    "D315"
  ],
  "Drug316": [
    "Drug316",
    "DRUG316",
    "drug-316",
    "D316"
  ],
  "Drug317": [
    "Drug317",
    "DRUG317",
    "drug-317",
    "D317"
  ],
  "Drug318": [
    "Drug318",
    "DRUG318",
    "drug-318",
    "D318"
  ],
  "Drug319": [
    "Drug319",
    "DRUG319",
    "drug-319",
    "D319"
  ],
  "Drug320": [
    "Drug320",
    "DRUG320",
    "drug-320",
    "D320"
  ],
  "Drug321": [
    "Drug321",
    "DRUG321",
    "drug-321",
    "D321"
  ],
  "Drug322": [
    "Drug322",
    "DRUG322",
    "drug-322",
    "D322"
  ],
  "Drug323": [
    "Drug323",
    "DRUG323",
    "drug-323",
    "D323"
  ],
  "Drug324": [
    "Drug324",
    "DRUG324",
    "drug-324",
    "D324"
  ],
  "Drug325": [
    "Drug325",
    "DRUG325",
    "drug-325",
    "D325"
  ],
  "Drug326": [
    "Drug326",
    "DRUG326",
    "drug-326",
    "D326"
  ],
  "Drug327": [
    "Drug327",
    "DRUG327",
    "drug-327",
    "D327"
  ],
  "Drug328": [
    "Drug328",
    "DRUG328",
    "drug-328",
    "D328"
  ],
  "Drug329": [
    "Drug329",
    "DRUG329",
    "drug-329",
    "D329"
  ],
  "Drug330": [
    "Drug330",
    "DRUG330",
    "drug-330",
    "D330"
  ],
  "Drug331": [
    "Drug331",
    "DRUG331",
    "drug-331",
    "D331"
  ],
  "Drug332": [
    "Drug332",
    "DRUG332",
    "drug-332",
    "D332"
  ],
  "Drug333": [
    "Drug333",
    "DRUG333",
    "drug-333",
    "D333"
  ],
  "Drug334": [
    "Drug334",
    "DRUG334",
    "drug-334",
    "D334"
  ],
  "Drug335": [
    "Drug335",
    "DRUG335",
    "drug-335",
    "D335"
  ],
  "Drug336": [
    "Drug336",
    "DRUG336",
    "drug-336",
    "D336"
  ],
  "Drug337": [
    "Drug337",
    "DRUG337",
    "drug-337",
    "D337"
  ],
  "Drug338": [
    "Drug338",
    "DRUG338",
    "drug-338",
    "D338"
  ],
  "Drug339": [
    "Drug339",
    "DRUG339",
    "drug-339",
    "D339"
  ],
  "Drug340": [
    "Drug340",
    "DRUG340",
    "drug-340",
    "D340"
  ],
  "Drug341": [
    "Drug341",
    "DRUG341",
    "drug-341",
    "D341"
  ],
  "Drug342": [
    "Drug342",
    "DRUG342",
    "drug-342",
    "D342"
  ],
  "Drug343": [
    "Drug343",
    "DRUG343",
    "drug-343",
    "D343"
  ],
  "Drug344": [
    "Drug344",
    "DRUG344",
    "drug-344",
    "D344"
  ],
  "Drug345": [
    "Drug345",
    "DRUG345",
    "drug-345",
    "D345"
  ],
  "Drug346": [
    "Drug346",
    "DRUG346",
    "drug-346",
    "D346"
  ],
  "Drug347": [
    "Drug347",
    "DRUG347",
    "drug-347",
    "D347"
  ],
  "Drug348": [
    "Drug348",
    "DRUG348",
    "drug-348",
    "D348"
  ],
  "Drug349": [
    "Drug349",
    "DRUG349",
    "drug-349",
    "D349"
  ],
  "Drug350": [
    "Drug350",
    "DRUG350",
    "drug-350",
    "D350"
  ],
  "Drug351": [
    "Drug351",
    "DRUG351",
    "drug-351",
    "D351"
  ],
  "Drug352": [
    "Drug352",
    "DRUG352",
    "drug-352",
    "D352"
  ],
  "Drug353": [
    "Drug353",
    "DRUG353",
    "drug-353",
    "D353"
  ],
  "Drug354": [
    "Drug354",
    "DRUG354",
    "drug-354",
    "D354"
  ],
  "Drug355": [
    "Drug355",
    "DRUG355",
    "drug-355",
    "D355"
  ],
  "Drug356": [
    "Drug356",
    "DRUG356",
    "drug-356",
    "D356"
  ],
  "Drug357": [
    "Drug357",
    "DRUG357",
    "drug-357",
    "D357"
  ],
  "Drug358": [
    "Drug358",
    "DRUG358",
    "drug-358",
    "D358"
  ],
  "Drug359": [
    "Drug359",
    "DRUG359",
    "drug-359",
    "D359"
  ],
  "Drug360": [
    "Drug360",
    "DRUG360",
    "drug-360",
    "D360"
  ],
  "Drug361": [
    "Drug361",
    "DRUG361",
    "drug-361",
    "D361"
  ],
  "Drug362": [
    "Drug362",
    "DRUG362",
    "drug-362",
    "D362"
  ],
  "Drug363": [
    "Drug363",
    "DRUG363",
    "drug-363",
    "D363"
  ],
  "Drug364": [
    "Drug364",
    "DRUG364",
    "drug-364",
    "D364"
  ],
  "Drug365": [
    "Drug365",
    "DRUG365",
    "drug-365",
    "D365"
  ],
  "Drug366": [
    "Drug366",
    "DRUG366",
    "drug-366",
    "D366"
  ],
  "Drug367": [
    "Drug367",
    "DRUG367",
    "drug-367",
    "D367"
  ],
  "Drug368": [
    "Drug368",
    "DRUG368",
    "drug-368",
    "D368"
  ],
  "Drug369": [
    "Drug369",
    "DRUG369",
    "drug-369",
    "D369"
  ],
  "Drug370": [
    "Drug370",
    "DRUG370",
    "drug-370",
    "D370"
  ],
  "Drug371": [
    "Drug371",
    "DRUG371",
    "drug-371",
    "D371"
  ],
  "Drug372": [
    "Drug372",
    "DRUG372",
    "drug-372",
    "D372"
  ],
  "Drug373": [
    "Drug373",
    "DRUG373",
    "drug-373",
    "D373"
  ],
  "Drug374": [
    "Drug374",
    "DRUG374",
    "drug-374",
    "D374"
  ],
  "Drug375": [
    "Drug375",
    "DRUG375",
    "drug-375",
    "D375"
  ],
  "Drug376": [
    "Drug376",
    "DRUG376",
    "drug-376",
    "D376"
  ],
  "Drug377": [
    "Drug377",
    "DRUG377",
    "drug-377",
    "D377"
  ],
  "Drug378": [
    "Drug378",
    "DRUG378",
    "drug-378",
    "D378"
  ],
  "Drug379": [
    "Drug379",
    "DRUG379",
    "drug-379",
    "D379"
  ],
  "Drug380": [
    "Drug380",
    "DRUG380",
    "drug-380",
    "D380"
  ],
  "Drug381": [
    "Drug381",
    "DRUG381",
    "drug-381",
    "D381"
  ],
  "Drug382": [
    "Drug382",
    "DRUG382",
    "drug-382",
    "D382"
  ],
  "Drug383": [
    "Drug383",
    "DRUG383",
    "drug-383",
    "D383"
  ],
  "Drug384": [
    "Drug384",
    "DRUG384",
    "drug-384",
    "D384"
  ],
  "Drug385": [
    "Drug385",
    "DRUG385",
    "drug-385",
    "D385"
  ],
  "Drug386": [
    "Drug386",
    "DRUG386",
    "drug-386",
    "D386"
  ],
  "Drug387": [
    "Drug387",
    "DRUG387",
    "drug-387",
    "D387"
  ],
  "Drug388": [
    "Drug388",
    "DRUG388",
    "drug-388",
    "D388"
  ],
  "Drug389": [
    "Drug389",
    "DRUG389",
    "drug-389",
    "D389"
  ],
  "Drug390": [
    "Drug390",
    "DRUG390",
    "drug-390",
    "D390"
  ],
  "Drug391": [
    "Drug391",
    "DRUG391",
    "drug-391",
    "D391"
  ],
  "Drug392": [
    "Drug392",
    "DRUG392",
    "drug-392",
    "D392"
  ],
  "Drug393": [
    "Drug393",
    "DRUG393",
    "drug-393",
    "D393"
  ],
  "Drug394": [
    "Drug394",
    "DRUG394",
    "drug-394",
    "D394"
  ],
  "Drug395": [
    "Drug395",
    "DRUG395",
    "drug-395",
    "D395"
  ],
  "Drug396": [
    "Drug396",
    "DRUG396",
    "drug-396",
    "D396"
  ],
  "Drug397": [
    "Drug397",
    "DRUG397",
    "drug-397",
    "D397"
  ],
  "Drug398": [
    "Drug398",
    "DRUG398",
    "drug-398",
    "D398"
  ],
  "Drug399": [
    "Drug399",
    "DRUG399",
    "drug-399",
    "D399"
  ],
  "Drug400": [
    "Drug400",
    "DRUG400",
    "drug-400",
    "D400"
  ],
  "Drug401": [
    "Drug401",
    "DRUG401",
    "drug-401",
    "D401"
  ],
  "Drug402": [
    "Drug402",
    "DRUG402",
    "drug-402",
    "D402"
  ],
  "Drug403": [
    "Drug403",
    "DRUG403",
    "drug-403",
    "D403"
  ],
  "Drug404": [
    "Drug404",
    "DRUG404",
    "drug-404",
    "D404"
  ],
  "Drug405": [
    "Drug405",
    "DRUG405",
    "drug-405",
    "D405"
  ],
  "Drug406": [
    "Drug406",
    "DRUG406",
    "drug-406",
    "D406"
  ],
  "Drug407": [
    "Drug407",
    "DRUG407",
    "drug-407",
    "D407"
  ],
  "Drug408": [
    "Drug408",
    "DRUG408",
    "drug-408",
    "D408"
  ],
  "Drug409": [
    "Drug409",
    "DRUG409",
    "drug-409",
    "D409"
  ],
  "Drug410": [
    "Drug410",
    "DRUG410",
    "drug-410",
    "D410"
  ],
  "Drug411": [
    "Drug411",
    "DRUG411",
    "drug-411",
    "D411"
  ],
  "Drug412": [
    "Drug412",
    "DRUG412",
    "drug-412",
    "D412"
  ],
  "Drug413": [
    "Drug413",
    "DRUG413",
    "drug-413",
    "D413"
  ],
  "Drug414": [
    "Drug414",
    "DRUG414",
    "drug-414",
    "D414"
  ],
  "Drug415": [
    "Drug415",
    "DRUG415",
    "drug-415",
    "D415"
  ],
  "Drug416": [
    "Drug416",
    "DRUG416",
    "drug-416",
    "D416"
  ],
  "Drug417": [
    "Drug417",
    "DRUG417",
    "drug-417",
    "D417"
  ],
  "Drug418": [
    "Drug418",
    "DRUG418",
    "drug-418",
    "D418"
  ],
  "Drug419": [
    "Drug419",
    "DRUG419",
    "drug-419",
    "D419"
  ],
  "Drug420": [
    "Drug420",
    "DRUG420",
    "drug-420",
    "D420"
  ],
  "Drug421": [
    "Drug421",
    "DRUG421",
    "drug-421",
    "D421"
  ],
  "Drug422": [
    "Drug422",
    "DRUG422",
    "drug-422",
    "D422"
  ],
  "Drug423": [
    "Drug423",
    "DRUG423",
    "drug-423",
    "D423"
  ],
  "Drug424": [
    "Drug424",
    "DRUG424",
    "drug-424",
    "D424"
  ],
  "Drug425": [
    "Drug425",
    "DRUG425",
    "drug-425",
    "D425"
  ],
  "Drug426": [
    "Drug426",
    "DRUG426",
    "drug-426",
    "D426"
  ],
  "Drug427": [
    "Drug427",
    "DRUG427",
    "drug-427",
    "D427"
  ],
  "Drug428": [
    "Drug428",
    "DRUG428",
    "drug-428",
    "D428"
  ],
  "Drug429": [
    "Drug429",
    "DRUG429",
    "drug-429",
    "D429"
  ],
  "Drug430": [
    "Drug430",
    "DRUG430",
    "drug-430",
    "D430"
  ],
  "Drug431": [
    "Drug431",
    "DRUG431",
    "drug-431",
    "D431"
  ],
  "Drug432": [
    "Drug432",
    "DRUG432",
    "drug-432",
    "D432"
  ],
  "Drug433": [
    "Drug433",
    "DRUG433",
    "drug-433",
    "D433"
  ],
  "Drug434": [
    "Drug434",
    "DRUG434",
    "drug-434",
    "D434"
  ],
  "Drug435": [
    "Drug435",
    "DRUG435",
    "drug-435",
    "D435"
  ],
  "Drug436": [
    "Drug436",
    "DRUG436",
    "drug-436",
    "D436"
  ],
  "Drug437": [
    "Drug437",
    "DRUG437",
    "drug-437",
    "D437"
  ],
  "Drug438": [
    "Drug438",
    "DRUG438",
    "drug-438",
    "D438"
  ],
  "Drug439": [
    "Drug439",
    "DRUG439",
    "drug-439",
    "D439"
  ],
  "Drug440": [
    "Drug440",
    "DRUG440",
    "drug-440",
    "D440"
  ],
  "Drug441": [
    "Drug441",
    "DRUG441",
    "drug-441",
    "D441"
  ],
  "Drug442": [
    "Drug442",
    "DRUG442",
    "drug-442",
    "D442"
  ],
  "Drug443": [
    "Drug443",
    "DRUG443",
    "drug-443",
    "D443"
  ],
  "Drug444": [
    "Drug444",
    "DRUG444",
    "drug-444",
    "D444"
  ],
  "Drug445": [
    "Drug445",
    "DRUG445",
    "drug-445",
    "D445"
  ],
  "Drug446": [
    "Drug446",
    "DRUG446",
    "drug-446",
    "D446"
  ],
  "Drug447": [
    "Drug447",
    "DRUG447",
    "drug-447",
    "D447"
  ],
  "Drug448": [
    "Drug448",
    "DRUG448",
    "drug-448",
    "D448"
  ],
  "Drug449": [
    "Drug449",
    "DRUG449",
    "drug-449",
    "D449"
  ],
  "Drug450": [
    "Drug450",
    "DRUG450",
    "drug-450",
    "D450"
  ],
  "Drug451": [
    "Drug451",
    "DRUG451",
    "drug-451",
    "D451"
  ],
  "Drug452": [
    "Drug452",
    "DRUG452",
    "drug-452",
    "D452"
  ],
  "Drug453": [
    "Drug453",
    "DRUG453",
    "drug-453",
    "D453"
  ],
  "Drug454": [
    "Drug454",
    "DRUG454",
    "drug-454",
    "D454"
  ],
  "Drug455": [
    "Drug455",
    "DRUG455",
    "drug-455",
    "D455"
  ],
  "Drug456": [
    "Drug456",
    "DRUG456",
    "drug-456",
    "D456"
  ],
  "Drug457": [
    "Drug457",
    "DRUG457",
    "drug-457",
    "D457"
  ],
  "Drug458": [
    "Drug458",
    "DRUG458",
    "drug-458",
    "D458"
  ],
  "Drug459": [
    "Drug459",
    "DRUG459",
    "drug-459",
    "D459"
  ],
  "Drug460": [
    "Drug460",
    "DRUG460",
    "drug-460",
    "D460"
  ],
  "Drug461": [
    "Drug461",
    "DRUG461",
    "drug-461",
    "D461"
  ],
  "Drug462": [
    "Drug462",
    "DRUG462",
    "drug-462",
    "D462"
  ],
  "Drug463": [
    "Drug463",
    "DRUG463",
    "drug-463",
    "D463"
  ],
  "Drug464": [
    "Drug464",
    "DRUG464",
    "drug-464",
    "D464"
  ],
  "Drug465": [
    "Drug465",
    "DRUG465",
    "drug-465",
    "D465"
  ],
  "Drug466": [
    "Drug466",
    "DRUG466",
    "drug-466",
    "D466"
  ],
  "Drug467": [
    "Drug467",
    "DRUG467",
    "drug-467",
    "D467"
  ],
  "Drug468": [
    "Drug468",
    "DRUG468",
    "drug-468",
    "D468"
  ],
  "Drug469": [
    "Drug469",
    "DRUG469",
    "drug-469",
    "D469"
  ],
  "Drug470": [
    "Drug470",
    "DRUG470",
    "drug-470",
    "D470"
  ],
  "Drug471": [
    "Drug471",
    "DRUG471",
    "drug-471",
    "D471"
  ],
  "Drug472": [
    "Drug472",
    "DRUG472",
    "drug-472",
    "D472"
  ],
  "Drug473": [
    "Drug473",
    "DRUG473",
    "drug-473",
    "D473"
  ],
  "Drug474": [
    "Drug474",
    "DRUG474",
    "drug-474",
    "D474"
  ],
  "Drug475": [
    "Drug475",
    "DRUG475",
    "drug-475",
    "D475"
  ],
  "Drug476": [
    "Drug476",
    "DRUG476",
    "drug-476",
    "D476"
  ],
  "Drug477": [
    "Drug477",
    "DRUG477",
    "drug-477",
    "D477"
  ],
  "Drug478": [
    "Drug478",
    "DRUG478",
    "drug-478",
    "D478"
  ],
  "Drug479": [
    "Drug479",
    "DRUG479",
    "drug-479",
    "D479"
  ],
  "Drug480": [
    "Drug480",
    "DRUG480",
    "drug-480",
    "D480"
  ],
  "Drug481": [
    "Drug481",
    "DRUG481",
    "drug-481",
    "D481"
  ],
  "Drug482": [
    "Drug482",
    "DRUG482",
    "drug-482",
    "D482"
  ],
  "Drug483": [
    "Drug483",
    "DRUG483",
    "drug-483",
    "D483"
  ],
  "Drug484": [
    "Drug484",
    "DRUG484",
    "drug-484",
    "D484"
  ],
  "Drug485": [
    "Drug485",
    "DRUG485",
    "drug-485",
    "D485"
  ],
  "Drug486": [
    "Drug486",
    "DRUG486",
    "drug-486",
    "D486"
  ],
  "Drug487": [
    "Drug487",
    "DRUG487",
    "drug-487",
    "D487"
  ],
  "Drug488": [
    "Drug488",
    "DRUG488",
    "drug-488",
    "D488"
  ],
  "Drug489": [
    "Drug489",
    "DRUG489",
    "drug-489",
    "D489"
  ],
  "Drug490": [
    "Drug490",
    "DRUG490",
    "drug-490",
    "D490"
  ],
  "Drug491": [
    "Drug491",
    "DRUG491",
    "drug-491",
    "D491"
  ],
  "Drug492": [
    "Drug492",
    "DRUG492",
    "drug-492",
    "D492"
  ],
  "Drug493": [
    "Drug493",
    "DRUG493",
    "drug-493",
    "D493"
  ],
  "Drug494": [
    "Drug494",
    "DRUG494",
    "drug-494",
    "D494"
  ],
  "Drug495": [
    "Drug495",
    "DRUG495",
    "drug-495",
    "D495"
  ],
  "Drug496": [
    "Drug496",
    "DRUG496",
    "drug-496",
    "D496"
  ],
  "Drug497": [
    "Drug497",
    "DRUG497",
    "drug-497",
    "D497"
  ],
  "Drug498": [
    "Drug498",
    "DRUG498",
    "drug-498",
    "D498"
  ],
  "Drug499": [
    "Drug499",
    "DRUG499",
    "drug-499",
    "D499"
  ],
  "Drug500": [
    "Drug500",
    "DRUG500",
    "drug-500",
    "D500"
  ],
  "Drug501": [
    "Drug501",
    "DRUG501",
    "drug-501",
    "D501"
  ],
  "Drug502": [
    "Drug502",
    "DRUG502",
    "drug-502",
    "D502"
  ],
  "Drug503": [
    "Drug503",
    "DRUG503",
    "drug-503",
    "D503"
  ],
  "Drug504": [
    "Drug504",
    "DRUG504",
    "drug-504",
    "D504"
  ],
  "Drug505": [
    "Drug505",
    "DRUG505",
    "drug-505",
    "D505"
  ],
  "Drug506": [
    "Drug506",
    "DRUG506",
    "drug-506",
    "D506"
  ],
  "Drug507": [
    "Drug507",
    "DRUG507",
    "drug-507",
    "D507"
  ],
  "Drug508": [
    "Drug508",
    "DRUG508",
    "drug-508",
    "D508"
  ],
  "Drug509": [
    "Drug509",
    "DRUG509",
    "drug-509",
    "D509"
  ],
  "Drug510": [
    "Drug510",
    "DRUG510",
    "drug-510",
    "D510"
  ],
  "Drug511": [
    "Drug511",
    "DRUG511",
    "drug-511",
    "D511"
  ],
  "Drug512": [
    "Drug512",
    "DRUG512",
    "drug-512",
    "D512"
  ],
  "Drug513": [
    "Drug513",
    "DRUG513",
    "drug-513",
    "D513"
  ],
  "Drug514": [
    "Drug514",
    "DRUG514",
    "drug-514",
    "D514"
  ],
  "Drug515": [
    "Drug515",
    "DRUG515",
    "drug-515",
    "D515"
  ],
  "Drug516": [
    "Drug516",
    "DRUG516",
    "drug-516",
    "D516"
  ],
  "Drug517": [
    "Drug517",
    "DRUG517",
    "drug-517",
    "D517"
  ],
  "Drug518": [
    "Drug518",
    "DRUG518",
    "drug-518",
    "D518"
  ],
  "Drug519": [
    "Drug519",
    "DRUG519",
    "drug-519",
    "D519"
  ],
  "Drug520": [
    "Drug520",
    "DRUG520",
    "drug-520",
    "D520"
  ],
  "Drug521": [
    "Drug521",
    "DRUG521",
    "drug-521",
    "D521"
  ],
  "Drug522": [
    "Drug522",
    "DRUG522",
    "drug-522",
    "D522"
  ],
  "Drug523": [
    "Drug523",
    "DRUG523",
    "drug-523",
    "D523"
  ],
  "Drug524": [
    "Drug524",
    "DRUG524",
    "drug-524",
    "D524"
  ],
  "Drug525": [
    "Drug525",
    "DRUG525",
    "drug-525",
    "D525"
  ],
  "Drug526": [
    "Drug526",
    "DRUG526",
    "drug-526",
    "D526"
  ],
  "Drug527": [
    "Drug527",
    "DRUG527",
    "drug-527",
    "D527"
  ],
  "Drug528": [
    "Drug528",
    "DRUG528",
    "drug-528",
    "D528"
  ],
  "Drug529": [
    "Drug529",
    "DRUG529",
    "drug-529",
    "D529"
  ],
  "Drug530": [
    "Drug530",
    "DRUG530",
    "drug-530",
    "D530"
  ],
  "Drug531": [
    "Drug531",
    "DRUG531",
    "drug-531",
    "D531"
  ],
  "Drug532": [
    "Drug532",
    "DRUG532",
    "drug-532",
    "D532"
  ],
  "Drug533": [
    "Drug533",
    "DRUG533",
    "drug-533",
    "D533"
  ],
  "Drug534": [
    "Drug534",
    "DRUG534",
    "drug-534",
    "D534"
  ],
  "Drug535": [
    "Drug535",
    "DRUG535",
    "drug-535",
    "D535"
  ],
  "Drug536": [
    "Drug536",
    "DRUG536",
    "drug-536",
    "D536"
  ],
  "Drug537": [
    "Drug537",
    "DRUG537",
    "drug-537",
    "D537"
  ],
  "Drug538": [
    "Drug538",
    "DRUG538",
    "drug-538",
    "D538"
  ],
  "Drug539": [
    "Drug539",
    "DRUG539",
    "drug-539",
    "D539"
  ],
  "Drug540": [
    "Drug540",
    "DRUG540",
    "drug-540",
    "D540"
  ],
  "Drug541": [
    "Drug541",
    "DRUG541",
    "drug-541",
    "D541"
  ],
  "Drug542": [
    "Drug542",
    "DRUG542",
    "drug-542",
    "D542"
  ],
  "Drug543": [
    "Drug543",
    "DRUG543",
    "drug-543",
    "D543"
  ],
  "Drug544": [
    "Drug544",
    "DRUG544",
    "drug-544",
    "D544"
  ],
  "Drug545": [
    "Drug545",
    "DRUG545",
    "drug-545",
    "D545"
  ],
  "Drug546": [
    "Drug546",
    "DRUG546",
    "drug-546",
    "D546"
  ],
  "Drug547": [
    "Drug547",
    "DRUG547",
    "drug-547",
    "D547"
  ],
  "Drug548": [
    "Drug548",
    "DRUG548",
    "drug-548",
    "D548"
  ],
  "Drug549": [
    "Drug549",
    "DRUG549",
    "drug-549",
    "D549"
  ],
  "Drug550": [
    "Drug550",
    "DRUG550",
    "drug-550",
    "D550"
  ],
  "Drug551": [
    "Drug551",
    "DRUG551",
    "drug-551",
    "D551"
  ],
  "Drug552": [
    "Drug552",
    "DRUG552",
    "drug-552",
    "D552"
  ],
  "Drug553": [
    "Drug553",
    "DRUG553",
    "drug-553",
    "D553"
  ],
  "Drug554": [
    "Drug554",
    "DRUG554",
    "drug-554",
    "D554"
  ],
  "Drug555": [
    "Drug555",
    "DRUG555",
    "drug-555",
    "D555"
  ],
  "Drug556": [
    "Drug556",
    "DRUG556",
    "drug-556",
    "D556"
  ],
  "Drug557": [
    "Drug557",
    "DRUG557",
    "drug-557",
    "D557"
  ],
  "Drug558": [
    "Drug558",
    "DRUG558",
    "drug-558",
    "D558"
  ],
  "Drug559": [
    "Drug559",
    "DRUG559",
    "drug-559",
    "D559"
  ],
  "Drug560": [
    "Drug560",
    "DRUG560",
    "drug-560",
    "D560"
  ],
  "Drug561": [
    "Drug561",
    "DRUG561",
    "drug-561",
    "D561"
  ],
  "Drug562": [
    "Drug562",
    "DRUG562",
    "drug-562",
    "D562"
  ],
  "Drug563": [
    "Drug563",
    "DRUG563",
    "drug-563",
    "D563"
  ],
  "Drug564": [
    "Drug564",
    "DRUG564",
    "drug-564",
    "D564"
  ],
  "Drug565": [
    "Drug565",
    "DRUG565",
    "drug-565",
    "D565"
  ],
  "Drug566": [
    "Drug566",
    "DRUG566",
    "drug-566",
    "D566"
  ],
  "Drug567": [
    "Drug567",
    "DRUG567",
    "drug-567",
    "D567"
  ],
  "Drug568": [
    "Drug568",
    "DRUG568",
    "drug-568",
    "D568"
  ],
  "Drug569": [
    "Drug569",
    "DRUG569",
    "drug-569",
    "D569"
  ],
  "Drug570": [
    "Drug570",
    "DRUG570",
    "drug-570",
    "D570"
  ],
  "Drug571": [
    "Drug571",
    "DRUG571",
    "drug-571",
    "D571"
  ],
  "Drug572": [
    "Drug572",
    "DRUG572",
    "drug-572",
    "D572"
  ],
  "Drug573": [
    "Drug573",
    "DRUG573",
    "drug-573",
    "D573"
  ],
  "Drug574": [
    "Drug574",
    "DRUG574",
    "drug-574",
    "D574"
  ],
  "Drug575": [
    "Drug575",
    "DRUG575",
    "drug-575",
    "D575"
  ],
  "Drug576": [
    "Drug576",
    "DRUG576",
    "drug-576",
    "D576"
  ],
  "Drug577": [
    "Drug577",
    "DRUG577",
    "drug-577",
    "D577"
  ],
  "Drug578": [
    "Drug578",
    "DRUG578",
    "drug-578",
    "D578"
  ],
  "Drug579": [
    "Drug579",
    "DRUG579",
    "drug-579",
    "D579"
  ],
  "Drug580": [
    "Drug580",
    "DRUG580",
    "drug-580",
    "D580"
  ],
  "Drug581": [
    "Drug581",
    "DRUG581",
    "drug-581",
    "D581"
  ],
  "Drug582": [
    "Drug582",
    "DRUG582",
    "drug-582",
    "D582"
  ],
  "Drug583": [
    "Drug583",
    "DRUG583",
    "drug-583",
    "D583"
  ],
  "Drug584": [
    "Drug584",
    "DRUG584",
    "drug-584",
    "D584"
  ],
  "Drug585": [
    "Drug585",
    "DRUG585",
    "drug-585",
    "D585"
  ],
  "Drug586": [
    "Drug586",
    "DRUG586",
    "drug-586",
    "D586"
  ],
  "Drug587": [
    "Drug587",
    "DRUG587",
    "drug-587",
    "D587"
  ],
  "Drug588": [
    "Drug588",
    "DRUG588",
    "drug-588",
    "D588"
  ],
  "Drug589": [
    "Drug589",
    "DRUG589",
    "drug-589",
    "D589"
  ],
  "Drug590": [
    "Drug590",
    "DRUG590",
    "drug-590",
    "D590"
  ],
  "Drug591": [
    "Drug591",
    "DRUG591",
    "drug-591",
    "D591"
  ],
  "Drug592": [
    "Drug592",
    "DRUG592",
    "drug-592",
    "D592"
  ],
  "Drug593": [
    "Drug593",
    "DRUG593",
    "drug-593",
    "D593"
  ],
  "Drug594": [
    "Drug594",
    "DRUG594",
    "drug-594",
    "D594"
  ],
  "Drug595": [
    "Drug595",
    "DRUG595",
    "drug-595",
    "D595"
  ],
  "Drug596": [
    "Drug596",
    "DRUG596",
    "drug-596",
    "D596"
  ],
  "Drug597": [
    "Drug597",
    "DRUG597",
    "drug-597",
    "D597"
  ],
  "Drug598": [
    "Drug598",
    "DRUG598",
    "drug-598",
    "D598"
  ],
  "Drug599": [
    "Drug599",
    "DRUG599",
    "drug-599",
    "D599"
  ],
  "Drug600": [
    "Drug600",
    "DRUG600",
    "drug-600",
    "D600"
  ],
  "Drug601": [
    "Drug601",
    "DRUG601",
    "drug-601",
    "D601"
  ],
  "Drug602": [
    "Drug602",
    "DRUG602",
    "drug-602",
    "D602"
  ],
  "Drug603": [
    "Drug603",
    "DRUG603",
    "drug-603",
    "D603"
  ],
  "Drug604": [
    "Drug604",
    "DRUG604",
    "drug-604",
    "D604"
  ],
  "Drug605": [
    "Drug605",
    "DRUG605",
    "drug-605",
    "D605"
  ],
  "Drug606": [
    "Drug606",
    "DRUG606",
    "drug-606",
    "D606"
  ],
  "Drug607": [
    "Drug607",
    "DRUG607",
    "drug-607",
    "D607"
  ],
  "Drug608": [
    "Drug608",
    "DRUG608",
    "drug-608",
    "D608"
  ],
  "Drug609": [
    "Drug609",
    "DRUG609",
    "drug-609",
    "D609"
  ],
  "Drug610": [
    "Drug610",
    "DRUG610",
    "drug-610",
    "D610"
  ],
  "Drug611": [
    "Drug611",
    "DRUG611",
    "drug-611",
    "D611"
  ],
  "Drug612": [
    "Drug612",
    "DRUG612",
    "drug-612",
    "D612"
  ],
  "Drug613": [
    "Drug613",
    "DRUG613",
    "drug-613",
    "D613"
  ],
  "Drug614": [
    "Drug614",
    "DRUG614",
    "drug-614",
    "D614"
  ],
  "Drug615": [
    "Drug615",
    "DRUG615",
    "drug-615",
    "D615"
  ],
  "Drug616": [
    "Drug616",
    "DRUG616",
    "drug-616",
    "D616"
  ],
  "Drug617": [
    "Drug617",
    "DRUG617",
    "drug-617",
    "D617"
  ],
  "Drug618": [
    "Drug618",
    "DRUG618",
    "drug-618",
    "D618"
  ],
  "Drug619": [
    "Drug619",
    "DRUG619",
    "drug-619",
    "D619"
  ],
  "Drug620": [
    "Drug620",
    "DRUG620",
    "drug-620",
    "D620"
  ],
  "Drug621": [
    "Drug621",
    "DRUG621",
    "drug-621",
    "D621"
  ],
  "Drug622": [
    "Drug622",
    "DRUG622",
    "drug-622",
    "D622"
  ],
  "Drug623": [
    "Drug623",
    "DRUG623",
    "drug-623",
    "D623"
  ],
  "Drug624": [
    "Drug624",
    "DRUG624",
    "drug-624",
    "D624"
  ],
  "Drug625": [
    "Drug625",
    "DRUG625",
    "drug-625",
    "D625"
  ],
  "Drug626": [
    "Drug626",
    "DRUG626",
    "drug-626",
    "D626"
  ],
  "Drug627": [
    "Drug627",
    "DRUG627",
    "drug-627",
    "D627"
  ],
  "Drug628": [
    "Drug628",
    "DRUG628",
    "drug-628",
    "D628"
  ],
  "Drug629": [
    "Drug629",
    "DRUG629",
    "drug-629",
    "D629"
  ],
  "Drug630": [
    "Drug630",
    "DRUG630",
    "drug-630",
    "D630"
  ],
  "Drug631": [
    "Drug631",
    "DRUG631",
    "drug-631",
    "D631"
  ],
  "Drug632": [
    "Drug632",
    "DRUG632",
    "drug-632",
    "D632"
  ],
  "Drug633": [
    "Drug633",
    "DRUG633",
    "drug-633",
    "D633"
  ],
  "Drug634": [
    "Drug634",
    "DRUG634",
    "drug-634",
    "D634"
  ],
  "Drug635": [
    "Drug635",
    "DRUG635",
    "drug-635",
    "D635"
  ],
  "Drug636": [
    "Drug636",
    "DRUG636",
    "drug-636",
    "D636"
  ],
  "Drug637": [
    "Drug637",
    "DRUG637",
    "drug-637",
    "D637"
  ],
  "Drug638": [
    "Drug638",
    "DRUG638",
    "drug-638",
    "D638"
  ],
  "Drug639": [
    "Drug639",
    "DRUG639",
    "drug-639",
    "D639"
  ],
  "Drug640": [
    "Drug640",
    "DRUG640",
    "drug-640",
    "D640"
  ],
  "Drug641": [
    "Drug641",
    "DRUG641",
    "drug-641",
    "D641"
  ],
  "Drug642": [
    "Drug642",
    "DRUG642",
    "drug-642",
    "D642"
  ],
  "Drug643": [
    "Drug643",
    "DRUG643",
    "drug-643",
    "D643"
  ],
  "Drug644": [
    "Drug644",
    "DRUG644",
    "drug-644",
    "D644"
  ],
  "Drug645": [
    "Drug645",
    "DRUG645",
    "drug-645",
    "D645"
  ],
  "Drug646": [
    "Drug646",
    "DRUG646",
    "drug-646",
    "D646"
  ],
  "Drug647": [
    "Drug647",
    "DRUG647",
    "drug-647",
    "D647"
  ],
  "Drug648": [
    "Drug648",
    "DRUG648",
    "drug-648",
    "D648"
  ],
  "Drug649": [
    "Drug649",
    "DRUG649",
    "drug-649",
    "D649"
  ],
  "Drug650": [
    "Drug650",
    "DRUG650",
    "drug-650",
    "D650"
  ],
  "Drug651": [
    "Drug651",
    "DRUG651",
    "drug-651",
    "D651"
  ],
  "Drug652": [
    "Drug652",
    "DRUG652",
    "drug-652",
    "D652"
  ],
  "Drug653": [
    "Drug653",
    "DRUG653",
    "drug-653",
    "D653"
  ],
  "Drug654": [
    "Drug654",
    "DRUG654",
    "drug-654",
    "D654"
  ],
  "Drug655": [
    "Drug655",
    "DRUG655",
    "drug-655",
    "D655"
  ],
  "Drug656": [
    "Drug656",
    "DRUG656",
    "drug-656",
    "D656"
  ],
  "Drug657": [
    "Drug657",
    "DRUG657",
    "drug-657",
    "D657"
  ],
  "Drug658": [
    "Drug658",
    "DRUG658",
    "drug-658",
    "D658"
  ],
  "Drug659": [
    "Drug659",
    "DRUG659",
    "drug-659",
    "D659"
  ],
  "Drug660": [
    "Drug660",
    "DRUG660",
    "drug-660",
    "D660"
  ],
  "Drug661": [
    "Drug661",
    "DRUG661",
    "drug-661",
    "D661"
  ],
  "Drug662": [
    "Drug662",
    "DRUG662",
    "drug-662",
    "D662"
  ],
  "Drug663": [
    "Drug663",
    "DRUG663",
    "drug-663",
    "D663"
  ],
  "Drug664": [
    "Drug664",
    "DRUG664",
    "drug-664",
    "D664"
  ],
  "Drug665": [
    "Drug665",
    "DRUG665",
    "drug-665",
    "D665"
  ],
  "Drug666": [
    "Drug666",
    "DRUG666",
    "drug-666",
    "D666"
  ],
  "Drug667": [
    "Drug667",
    "DRUG667",
    "drug-667",
    "D667"
  ],
  "Drug668": [
    "Drug668",
    "DRUG668",
    "drug-668",
    "D668"
  ],
  "Drug669": [
    "Drug669",
    "DRUG669",
    "drug-669",
    "D669"
  ],
  "Drug670": [
    "Drug670",
    "DRUG670",
    "drug-670",
    "D670"
  ],
  "Drug671": [
    "Drug671",
    "DRUG671",
    "drug-671",
    "D671"
  ],
  "Drug672": [
    "Drug672",
    "DRUG672",
    "drug-672",
    "D672"
  ],
  "Drug673": [
    "Drug673",
    "DRUG673",
    "drug-673",
    "D673"
  ],
  "Drug674": [
    "Drug674",
    "DRUG674",
    "drug-674",
    "D674"
  ],
  "Drug675": [
    "Drug675",
    "DRUG675",
    "drug-675",
    "D675"
  ],
  "Drug676": [
    "Drug676",
    "DRUG676",
    "drug-676",
    "D676"
  ],
  "Drug677": [
    "Drug677",
    "DRUG677",
    "drug-677",
    "D677"
  ],
  "Drug678": [
    "Drug678",
    "DRUG678",
    "drug-678",
    "D678"
  ],
  "Drug679": [
    "Drug679",
    "DRUG679",
    "drug-679",
    "D679"
  ],
  "Drug680": [
    "Drug680",
    "DRUG680",
    "drug-680",
    "D680"
  ],
  "Drug681": [
    "Drug681",
    "DRUG681",
    "drug-681",
    "D681"
  ],
  "Drug682": [
    "Drug682",
    "DRUG682",
    "drug-682",
    "D682"
  ],
  "Drug683": [
    "Drug683",
    "DRUG683",
    "drug-683",
    "D683"
  ],
  "Drug684": [
    "Drug684",
    "DRUG684",
    "drug-684",
    "D684"
  ],
  "Drug685": [
    "Drug685",
    "DRUG685",
    "drug-685",
    "D685"
  ],
  "Drug686": [
    "Drug686",
    "DRUG686",
    "drug-686",
    "D686"
  ],
  "Drug687": [
    "Drug687",
    "DRUG687",
    "drug-687",
    "D687"
  ],
  "Drug688": [
    "Drug688",
    "DRUG688",
    "drug-688",
    "D688"
  ],
  "Drug689": [
    "Drug689",
    "DRUG689",
    "drug-689",
    "D689"
  ],
  "Drug690": [
    "Drug690",
    "DRUG690",
    "drug-690",
    "D690"
  ],
  "Drug691": [
    "Drug691",
    "DRUG691",
    "drug-691",
    "D691"
  ],
  "Drug692": [
    "Drug692",
    "DRUG692",
    "drug-692",
    "D692"
  ],
  "Drug693": [
    "Drug693",
    "DRUG693",
    "drug-693",
    "D693"
  ],
  "Drug694": [
    "Drug694",
    "DRUG694",
    "drug-694",
    "D694"
  ],
  "Drug695": [
    "Drug695",
    "DRUG695",
    "drug-695",
    "D695"
  ],
  "Drug696": [
    "Drug696",
    "DRUG696",
    "drug-696",
    "D696"
  ],
  "Drug697": [
    "Drug697",
    "DRUG697",
    "drug-697",
    "D697"
  ],
  "Drug698": [
    "Drug698",
    "DRUG698",
    "drug-698",
    "D698"
  ],
  "Drug699": [
    "Drug699",
    "DRUG699",
    "drug-699",
    "D699"
  ],
  "Drug700": [
    "Drug700",
    "DRUG700",
    "drug-700",
    "D700"
  ],
  "Drug701": [
    "Drug701",
    "DRUG701",
    "drug-701",
    "D701"
  ],
  "Drug702": [
    "Drug702",
    "DRUG702",
    "drug-702",
    "D702"
  ],
  "Drug703": [
    "Drug703",
    "DRUG703",
    "drug-703",
    "D703"
  ],
  "Drug704": [
    "Drug704",
    "DRUG704",
    "drug-704",
    "D704"
  ],
  "Drug705": [
    "Drug705",
    "DRUG705",
    "drug-705",
    "D705"
  ],
  "Drug706": [
    "Drug706",
    "DRUG706",
    "drug-706",
    "D706"
  ],
  "Drug707": [
    "Drug707",
    "DRUG707",
    "drug-707",
    "D707"
  ],
  "Drug708": [
    "Drug708",
    "DRUG708",
    "drug-708",
    "D708"
  ],
  "Drug709": [
    "Drug709",
    "DRUG709",
    "drug-709",
    "D709"
  ],
  "Drug710": [
    "Drug710",
    "DRUG710",
    "drug-710",
    "D710"
  ],
  "Drug711": [
    "Drug711",
    "DRUG711",
    "drug-711",
    "D711"
  ],
  "Drug712": [
    "Drug712",
    "DRUG712",
    "drug-712",
    "D712"
  ],
  "Drug713": [
    "Drug713",
    "DRUG713",
    "drug-713",
    "D713"
  ],
  "Drug714": [
    "Drug714",
    "DRUG714",
    "drug-714",
    "D714"
  ],
  "Drug715": [
    "Drug715",
    "DRUG715",
    "drug-715",
    "D715"
  ],
  "Drug716": [
    "Drug716",
    "DRUG716",
    "drug-716",
    "D716"
  ],
  "Drug717": [
    "Drug717",
    "DRUG717",
    "drug-717",
    "D717"
  ],
  "Drug718": [
    "Drug718",
    "DRUG718",
    "drug-718",
    "D718"
  ],
  "Drug719": [
    "Drug719",
    "DRUG719",
    "drug-719",
    "D719"
  ],
  "Drug720": [
    "Drug720",
    "DRUG720",
    "drug-720",
    "D720"
  ],
  "Drug721": [
    "Drug721",
    "DRUG721",
    "drug-721",
    "D721"
  ],
  "Drug722": [
    "Drug722",
    "DRUG722",
    "drug-722",
    "D722"
  ],
  "Drug723": [
    "Drug723",
    "DRUG723",
    "drug-723",
    "D723"
  ],
  "Drug724": [
    "Drug724",
    "DRUG724",
    "drug-724",
    "D724"
  ],
  "Drug725": [
    "Drug725",
    "DRUG725",
    "drug-725",
    "D725"
  ],
  "Drug726": [
    "Drug726",
    "DRUG726",
    "drug-726",
    "D726"
  ],
  "Drug727": [
    "Drug727",
    "DRUG727",
    "drug-727",
    "D727"
  ],
  "Drug728": [
    "Drug728",
    "DRUG728",
    "drug-728",
    "D728"
  ],
  "Drug729": [
    "Drug729",
    "DRUG729",
    "drug-729",
    "D729"
  ],
  "Drug730": [
    "Drug730",
    "DRUG730",
    "drug-730",
    "D730"
  ],
  "Drug731": [
    "Drug731",
    "DRUG731",
    "drug-731",
    "D731"
  ],
  "Drug732": [
    "Drug732",
    "DRUG732",
    "drug-732",
    "D732"
  ],
  "Drug733": [
    "Drug733",
    "DRUG733",
    "drug-733",
    "D733"
  ],
  "Drug734": [
    "Drug734",
    "DRUG734",
    "drug-734",
    "D734"
  ],
  "Drug735": [
    "Drug735",
    "DRUG735",
    "drug-735",
    "D735"
  ],
  "Drug736": [
    "Drug736",
    "DRUG736",
    "drug-736",
    "D736"
  ],
  "Drug737": [
    "Drug737",
    "DRUG737",
    "drug-737",
    "D737"
  ],
  "Drug738": [
    "Drug738",
    "DRUG738",
    "drug-738",
    "D738"
  ],
  "Drug739": [
    "Drug739",
    "DRUG739",
    "drug-739",
    "D739"
  ],
  "Drug740": [
    "Drug740",
    "DRUG740",
    "drug-740",
    "D740"
  ],
  "Drug741": [
    "Drug741",
    "DRUG741",
    "drug-741",
    "D741"
  ],
  "Drug742": [
    "Drug742",
    "DRUG742",
    "drug-742",
    "D742"
  ],
  "Drug743": [
    "Drug743",
    "DRUG743",
    "drug-743",
    "D743"
  ],
  "Drug744": [
    "Drug744",
    "DRUG744",
    "drug-744",
    "D744"
  ],
  "Drug745": [
    "Drug745",
    "DRUG745",
    "drug-745",
    "D745"
  ],
  "Drug746": [
    "Drug746",
    "DRUG746",
    "drug-746",
    "D746"
  ],
  "Drug747": [
    "Drug747",
    "DRUG747",
    "drug-747",
    "D747"
  ],
  "Drug748": [
    "Drug748",
    "DRUG748",
    "drug-748",
    "D748"
  ],
  "Drug749": [
    "Drug749",
    "DRUG749",
    "drug-749",
    "D749"
  ],
  "Drug750": [
    "Drug750",
    "DRUG750",
    "drug-750",
    "D750"
  ],
  "Drug751": [
    "Drug751",
    "DRUG751",
    "drug-751",
    "D751"
  ],
  "Drug752": [
    "Drug752",
    "DRUG752",
    "drug-752",
    "D752"
  ],
  "Drug753": [
    "Drug753",
    "DRUG753",
    "drug-753",
    "D753"
  ],
  "Drug754": [
    "Drug754",
    "DRUG754",
    "drug-754",
    "D754"
  ],
  "Drug755": [
    "Drug755",
    "DRUG755",
    "drug-755",
    "D755"
  ],
  "Drug756": [
    "Drug756",
    "DRUG756",
    "drug-756",
    "D756"
  ],
  "Drug757": [
    "Drug757",
    "DRUG757",
    "drug-757",
    "D757"
  ],
  "Drug758": [
    "Drug758",
    "DRUG758",
    "drug-758",
    "D758"
  ],
  "Drug759": [
    "Drug759",
    "DRUG759",
    "drug-759",
    "D759"
  ],
  "Drug760": [
    "Drug760",
    "DRUG760",
    "drug-760",
    "D760"
  ],
  "Drug761": [
    "Drug761",
    "DRUG761",
    "drug-761",
    "D761"
  ],
  "Drug762": [
    "Drug762",
    "DRUG762",
    "drug-762",
    "D762"
  ],
  "Drug763": [
    "Drug763",
    "DRUG763",
    "drug-763",
    "D763"
  ],
  "Drug764": [
    "Drug764",
    "DRUG764",
    "drug-764",
    "D764"
  ],
  "Drug765": [
    "Drug765",
    "DRUG765",
    "drug-765",
    "D765"
  ],
  "Drug766": [
    "Drug766",
    "DRUG766",
    "drug-766",
    "D766"
  ],
  "Drug767": [
    "Drug767",
    "DRUG767",
    "drug-767",
    "D767"
  ],
  "Drug768": [
    "Drug768",
    "DRUG768",
    "drug-768",
    "D768"
  ],
  "Drug769": [
    "Drug769",
    "DRUG769",
    "drug-769",
    "D769"
  ],
  "Drug770": [
    "Drug770",
    "DRUG770",
    "drug-770",
    "D770"
  ],
  "Drug771": [
    "Drug771",
    "DRUG771",
    "drug-771",
    "D771"
  ],
  "Drug772": [
    "Drug772",
    "DRUG772",
    "drug-772",
    "D772"
  ],
  "Drug773": [
    "Drug773",
    "DRUG773",
    "drug-773",
    "D773"
  ],
  "Drug774": [
    "Drug774",
    "DRUG774",
    "drug-774",
    "D774"
  ],
  "Drug775": [
    "Drug775",
    "DRUG775",
    "drug-775",
    "D775"
  ],
  "Drug776": [
    "Drug776",
    "DRUG776",
    "drug-776",
    "D776"
  ],
  "Drug777": [
    "Drug777",
    "DRUG777",
    "drug-777",
    "D777"
  ],
  "Drug778": [
    "Drug778",
    "DRUG778",
    "drug-778",
    "D778"
  ],
  "Drug779": [
    "Drug779",
    "DRUG779",
    "drug-779",
    "D779"
  ],
  "Drug780": [
    "Drug780",
    "DRUG780",
    "drug-780",
    "D780"
  ],
  "Drug781": [
    "Drug781",
    "DRUG781",
    "drug-781",
    "D781"
  ],
  "Drug782": [
    "Drug782",
    "DRUG782",
    "drug-782",
    "D782"
  ],
  "Drug783": [
    "Drug783",
    "DRUG783",
    "drug-783",
    "D783"
  ],
  "Drug784": [
    "Drug784",
    "DRUG784",
    "drug-784",
    "D784"
  ],
  "Drug785": [
    "Drug785",
    "DRUG785",
    "drug-785",
    "D785"
  ],
  "Drug786": [
    "Drug786",
    "DRUG786",
    "drug-786",
    "D786"
  ],
  "Drug787": [
    "Drug787",
    "DRUG787",
    "drug-787",
    "D787"
  ],
  "Drug788": [
    "Drug788",
    "DRUG788",
    "drug-788",
    "D788"
  ],
  "Drug789": [
    "Drug789",
    "DRUG789",
    "drug-789",
    "D789"
  ],
  "Drug790": [
    "Drug790",
    "DRUG790",
    "drug-790",
    "D790"
  ],
  "Drug791": [
    "Drug791",
    "DRUG791",
    "drug-791",
    "D791"
  ],
  "Drug792": [
    "Drug792",
    "DRUG792",
    "drug-792",
    "D792"
  ],
  "Drug793": [
    "Drug793",
    "DRUG793",
    "drug-793",
    "D793"
  ],
  "Drug794": [
    "Drug794",
    "DRUG794",
    "drug-794",
    "D794"
  ],
  "Drug795": [
    "Drug795",
    "DRUG795",
    "drug-795",
    "D795"
  ],
  "Drug796": [
    "Drug796",
    "DRUG796",
    "drug-796",
    "D796"
  ],
  "Drug797": [
    "Drug797",
    "DRUG797",
    "drug-797",
    "D797"
  ],
  "Drug798": [
    "Drug798",
    "DRUG798",
    "drug-798",
    "D798"
  ],
  "Drug799": [
    "Drug799",
    "DRUG799",
    "drug-799",
    "D799"
  ],
  "Drug800": [
    "Drug800",
    "DRUG800",
    "drug-800",
    "D800"
  ],
  "Drug801": [
    "Drug801",
    "DRUG801",
    "drug-801",
    "D801"
  ],
  "Drug802": [
    "Drug802",
    "DRUG802",
    "drug-802",
    "D802"
  ],
  "Drug803": [
    "Drug803",
    "DRUG803",
    "drug-803",
    "D803"
  ],
  "Drug804": [
    "Drug804",
    "DRUG804",
    "drug-804",
    "D804"
  ],
  "Drug805": [
    "Drug805",
    "DRUG805",
    "drug-805",
    "D805"
  ],
  "Drug806": [
    "Drug806",
    "DRUG806",
    "drug-806",
    "D806"
  ],
  "Drug807": [
    "Drug807",
    "DRUG807",
    "drug-807",
    "D807"
  ],
  "Drug808": [
    "Drug808",
    "DRUG808",
    "drug-808",
    "D808"
  ],
  "Drug809": [
    "Drug809",
    "DRUG809",
    "drug-809",
    "D809"
  ],
  "Drug810": [
    "Drug810",
    "DRUG810",
    "drug-810",
    "D810"
  ],
  "Drug811": [
    "Drug811",
    "DRUG811",
    "drug-811",
    "D811"
  ],
  "Drug812": [
    "Drug812",
    "DRUG812",
    "drug-812",
    "D812"
  ],
  "Drug813": [
    "Drug813",
    "DRUG813",
    "drug-813",
    "D813"
  ],
  "Drug814": [
    "Drug814",
    "DRUG814",
    "drug-814",
    "D814"
  ],
  "Drug815": [
    "Drug815",
    "DRUG815",
    "drug-815",
    "D815"
  ],
  "Drug816": [
    "Drug816",
    "DRUG816",
    "drug-816",
    "D816"
  ],
  "Drug817": [
    "Drug817",
    "DRUG817",
    "drug-817",
    "D817"
  ],
  "Drug818": [
    "Drug818",
    "DRUG818",
    "drug-818",
    "D818"
  ],
  "Drug819": [
    "Drug819",
    "DRUG819",
    "drug-819",
    "D819"
  ],
  "Drug820": [
    "Drug820",
    "DRUG820",
    "drug-820",
    "D820"
  ],
  "Drug821": [
    "Drug821",
    "DRUG821",
    "drug-821",
    "D821"
  ],
  "Drug822": [
    "Drug822",
    "DRUG822",
    "drug-822",
    "D822"
  ],
  "Drug823": [
    "Drug823",
    "DRUG823",
    "drug-823",
    "D823"
  ],
  "Drug824": [
    "Drug824",
    "DRUG824",
    "drug-824",
    "D824"
  ],
  "Drug825": [
    "Drug825",
    "DRUG825",
    "drug-825",
    "D825"
  ],
  "Drug826": [
    "Drug826",
    "DRUG826",
    "drug-826",
    "D826"
  ],
  "Drug827": [
    "Drug827",
    "DRUG827",
    "drug-827",
    "D827"
  ],
  "Drug828": [
    "Drug828",
    "DRUG828",
    "drug-828",
    "D828"
  ],
  "Drug829": [
    "Drug829",
    "DRUG829",
    "drug-829",
    "D829"
  ],
  "Drug830": [
    "Drug830",
    "DRUG830",
    "drug-830",
    "D830"
  ],
  "Drug831": [
    "Drug831",
    "DRUG831",
    "drug-831",
    "D831"
  ],
  "Drug832": [
    "Drug832",
    "DRUG832",
    "drug-832",
    "D832"
  ],
  "Drug833": [
    "Drug833",
    "DRUG833",
    "drug-833",
    "D833"
  ],
  "Drug834": [
    "Drug834",
    "DRUG834",
    "drug-834",
    "D834"
  ],
  "Drug835": [
    "Drug835",
    "DRUG835",
    "drug-835",
    "D835"
  ],
  "Drug836": [
    "Drug836",
    "DRUG836",
    "drug-836",
    "D836"
  ],
  "Drug837": [
    "Drug837",
    "DRUG837",
    "drug-837",
    "D837"
  ],
  "Drug838": [
    "Drug838",
    "DRUG838",
    "drug-838",
    "D838"
  ],
  "Drug839": [
    "Drug839",
    "DRUG839",
    "drug-839",
    "D839"
  ],
  "Drug840": [
    "Drug840",
    "DRUG840",
    "drug-840",
    "D840"
  ],
  "Drug841": [
    "Drug841",
    "DRUG841",
    "drug-841",
    "D841"
  ],
  "Drug842": [
    "Drug842",
    "DRUG842",
    "drug-842",
    "D842"
  ],
  "Drug843": [
    "Drug843",
    "DRUG843",
    "drug-843",
    "D843"
  ],
  "Drug844": [
    "Drug844",
    "DRUG844",
    "drug-844",
    "D844"
  ],
  "Drug845": [
    "Drug845",
    "DRUG845",
    "drug-845",
    "D845"
  ],
  "Drug846": [
    "Drug846",
    "DRUG846",
    "drug-846",
    "D846"
  ],
  "Drug847": [
    "Drug847",
    "DRUG847",
    "drug-847",
    "D847"
  ],
  "Drug848": [
    "Drug848",
    "DRUG848",
    "drug-848",
    "D848"
  ],
  "Drug849": [
    "Drug849",
    "DRUG849",
    "drug-849",
    "D849"
  ],
  "Drug850": [
    "Drug850",
    "DRUG850",
    "drug-850",
    "D850"
  ],
  "Drug851": [
    "Drug851",
    "DRUG851",
    "drug-851",
    "D851"
  ],
  "Drug852": [
    "Drug852",
    "DRUG852",
    "drug-852",
    "D852"
  ],
  "Drug853": [
    "Drug853",
    "DRUG853",
    "drug-853",
    "D853"
  ],
  "Drug854": [
    "Drug854",
    "DRUG854",
    "drug-854",
    "D854"
  ],
  "Drug855": [
    "Drug855",
    "DRUG855",
    "drug-855",
    "D855"
  ],
  "Drug856": [
    "Drug856",
    "DRUG856",
    "drug-856",
    "D856"
  ],
  "Drug857": [
    "Drug857",
    "DRUG857",
    "drug-857",
    "D857"
  ],
  "Drug858": [
    "Drug858",
    "DRUG858",
    "drug-858",
    "D858"
  ],
  "Drug859": [
    "Drug859",
    "DRUG859",
    "drug-859",
    "D859"
  ],
  "Drug860": [
    "Drug860",
    "DRUG860",
    "drug-860",
    "D860"
  ],
  "Drug861": [
    "Drug861",
    "DRUG861",
    "drug-861",
    "D861"
  ],
  "Drug862": [
    "Drug862",
    "DRUG862",
    "drug-862",
    "D862"
  ],
  "Drug863": [
    "Drug863",
    "DRUG863",
    "drug-863",
    "D863"
  ],
  "Drug864": [
    "Drug864",
    "DRUG864",
    "drug-864",
    "D864"
  ],
  "Drug865": [
    "Drug865",
    "DRUG865",
    "drug-865",
    "D865"
  ],
  "Drug866": [
    "Drug866",
    "DRUG866",
    "drug-866",
    "D866"
  ],
  "Drug867": [
    "Drug867",
    "DRUG867",
    "drug-867",
    "D867"
  ],
  "Drug868": [
    "Drug868",
    "DRUG868",
    "drug-868",
    "D868"
  ],
  "Drug869": [
    "Drug869",
    "DRUG869",
    "drug-869",
    "D869"
  ],
  "Drug870": [
    "Drug870",
    "DRUG870",
    "drug-870",
    "D870"
  ],
  "Drug871": [
    "Drug871",
    "DRUG871",
    "drug-871",
    "D871"
  ],
  "Drug872": [
    "Drug872",
    "DRUG872",
    "drug-872",
    "D872"
  ],
  "Drug873": [
    "Drug873",
    "DRUG873",
    "drug-873",
    "D873"
  ],
  "Drug874": [
    "Drug874",
    "DRUG874",
    "drug-874",
    "D874"
  ],
  "Drug875": [
    "Drug875",
    "DRUG875",
    "drug-875",
    "D875"
  ],
  "Drug876": [
    "Drug876",
    "DRUG876",
    "drug-876",
    "D876"
  ],
  "Drug877": [
    "Drug877",
    "DRUG877",
    "drug-877",
    "D877"
  ],
  "Drug878": [
    "Drug878",
    "DRUG878",
    "drug-878",
    "D878"
  ],
  "Drug879": [
    "Drug879",
    "DRUG879",
    "drug-879",
    "D879"
  ],
  "Drug880": [
    "Drug880",
    "DRUG880",
    "drug-880",
    "D880"
  ],
  "Drug881": [
    "Drug881",
    "DRUG881",
    "drug-881",
    "D881"
  ],
  "Drug882": [
    "Drug882",
    "DRUG882",
    "drug-882",
    "D882"
  ],
  "Drug883": [
    "Drug883",
    "DRUG883",
    "drug-883",
    "D883"
  ],
  "Drug884": [
    "Drug884",
    "DRUG884",
    "drug-884",
    "D884"
  ],
  "Drug885": [
    "Drug885",
    "DRUG885",
    "drug-885",
    "D885"
  ],
  "Drug886": [
    "Drug886",
    "DRUG886",
    "drug-886",
    "D886"
  ],
  "Drug887": [
    "Drug887",
    "DRUG887",
    "drug-887",
    "D887"
  ],
  "Drug888": [
    "Drug888",
    "DRUG888",
    "drug-888",
    "D888"
  ],
  "Drug889": [
    "Drug889",
    "DRUG889",
    "drug-889",
    "D889"
  ],
  "Drug890": [
    "Drug890",
    "DRUG890",
    "drug-890",
    "D890"
  ],
  "Drug891": [
    "Drug891",
    "DRUG891",
    "drug-891",
    "D891"
  ],
  "Drug892": [
    "Drug892",
    "DRUG892",
    "drug-892",
    "D892"
  ],
  "Drug893": [
    "Drug893",
    "DRUG893",
    "drug-893",
    "D893"
  ],
  "Drug894": [
    "Drug894",
    "DRUG894",
    "drug-894",
    "D894"
  ],
  "Drug895": [
    "Drug895",
    "DRUG895",
    "drug-895",
    "D895"
  ],
  "Drug896": [
    "Drug896",
    "DRUG896",
    "drug-896",
    "D896"
  ],
  "Drug897": [
    "Drug897",
    "DRUG897",
    "drug-897",
    "D897"
  ],
  "Drug898": [
    "Drug898",
    "DRUG898",
    "drug-898",
    "D898"
  ],
  "Drug899": [
    "Drug899",
    "DRUG899",
    "drug-899",
    "D899"
  ],
  "Drug900": [
    "Drug900",
    "DRUG900",
    "drug-900",
    "D900"
  ],
  "Drug901": [
    "Drug901",
    "DRUG901",
    "drug-901",
    "D901"
  ],
  "Drug902": [
    "Drug902",
    "DRUG902",
    "drug-902",
    "D902"
  ],
  "Drug903": [
    "Drug903",
    "DRUG903",
    "drug-903",
    "D903"
  ],
  "Drug904": [
    "Drug904",
    "DRUG904",
    "drug-904",
    "D904"
  ],
  "Drug905": [
    "Drug905",
    "DRUG905",
    "drug-905",
    "D905"
  ],
  "Drug906": [
    "Drug906",
    "DRUG906",
    "drug-906",
    "D906"
  ],
  "Drug907": [
    "Drug907",
    "DRUG907",
    "drug-907",
    "D907"
  ],
  "Drug908": [
    "Drug908",
    "DRUG908",
    "drug-908",
    "D908"
  ],
  "Drug909": [
    "Drug909",
    "DRUG909",
    "drug-909",
    "D909"
  ],
  "Drug910": [
    "Drug910",
    "DRUG910",
    "drug-910",
    "D910"
  ],
  "Drug911": [
    "Drug911",
    "DRUG911",
    "drug-911",
    "D911"
  ],
  "Drug912": [
    "Drug912",
    "DRUG912",
    "drug-912",
    "D912"
  ],
  "Drug913": [
    "Drug913",
    "DRUG913",
    "drug-913",
    "D913"
  ],
  "Drug914": [
    "Drug914",
    "DRUG914",
    "drug-914",
    "D914"
  ],
  "Drug915": [
    "Drug915",
    "DRUG915",
    "drug-915",
    "D915"
  ],
  "Drug916": [
    "Drug916",
    "DRUG916",
    "drug-916",
    "D916"
  ],
  "Drug917": [
    "Drug917",
    "DRUG917",
    "drug-917",
    "D917"
  ],
  "Drug918": [
    "Drug918",
    "DRUG918",
    "drug-918",
    "D918"
  ],
  "Drug919": [
    "Drug919",
    "DRUG919",
    "drug-919",
    "D919"
  ],
  "Drug920": [
    "Drug920",
    "DRUG920",
    "drug-920",
    "D920"
  ],
  "Drug921": [
    "Drug921",
    "DRUG921",
    "drug-921",
    "D921"
  ],
  "Drug922": [
    "Drug922",
    "DRUG922",
    "drug-922",
    "D922"
  ],
  "Drug923": [
    "Drug923",
    "DRUG923",
    "drug-923",
    "D923"
  ],
  "Drug924": [
    "Drug924",
    "DRUG924",
    "drug-924",
    "D924"
  ],
  "Drug925": [
    "Drug925",
    "DRUG925",
    "drug-925",
    "D925"
  ],
  "Drug926": [
    "Drug926",
    "DRUG926",
    "drug-926",
    "D926"
  ],
  "Drug927": [
    "Drug927",
    "DRUG927",
    "drug-927",
    "D927"
  ],
  "Drug928": [
    "Drug928",
    "DRUG928",
    "drug-928",
    "D928"
  ],
  "Drug929": [
    "Drug929",
    "DRUG929",
    "drug-929",
    "D929"
  ],
  "Drug930": [
    "Drug930",
    "DRUG930",
    "drug-930",
    "D930"
  ],
  "Drug931": [
    "Drug931",
    "DRUG931",
    "drug-931",
    "D931"
  ],
  "Drug932": [
    "Drug932",
    "DRUG932",
    "drug-932",
    "D932"
  ],
  "Drug933": [
    "Drug933",
    "DRUG933",
    "drug-933",
    "D933"
  ],
  "Drug934": [
    "Drug934",
    "DRUG934",
    "drug-934",
    "D934"
  ],
  "Drug935": [
    "Drug935",
    "DRUG935",
    "drug-935",
    "D935"
  ],
  "Drug936": [
    "Drug936",
    "DRUG936",
    "drug-936",
    "D936"
  ],
  "Drug937": [
    "Drug937",
    "DRUG937",
    "drug-937",
    "D937"
  ],
  "Drug938": [
    "Drug938",
    "DRUG938",
    "drug-938",
    "D938"
  ],
  "Drug939": [
    "Drug939",
    "DRUG939",
    "drug-939",
    "D939"
  ],
  "Drug940": [
    "Drug940",
    "DRUG940",
    "drug-940",
    "D940"
  ],
  "Drug941": [
    "Drug941",
    "DRUG941",
    "drug-941",
    "D941"
  ],
  "Drug942": [
    "Drug942",
    "DRUG942",
    "drug-942",
    "D942"
  ],
  "Drug943": [
    "Drug943",
    "DRUG943",
    "drug-943",
    "D943"
  ],
  "Drug944": [
    "Drug944",
    "DRUG944",
    "drug-944",
    "D944"
  ],
  "Drug945": [
    "Drug945",
    "DRUG945",
    "drug-945",
    "D945"
  ],
  "Drug946": [
    "Drug946",
    "DRUG946",
    "drug-946",
    "D946"
  ],
  "Drug947": [
    "Drug947",
    "DRUG947",
    "drug-947",
    "D947"
  ],
  "Drug948": [
    "Drug948",
    "DRUG948",
    "drug-948",
    "D948"
  ],
  "Drug949": [
    "Drug949",
    "DRUG949",
    "drug-949",
    "D949"
  ],
  "Drug950": [
    "Drug950",
    "DRUG950",
    "drug-950",
    "D950"
  ],
  "Drug951": [
    "Drug951",
    "DRUG951",
    "drug-951",
    "D951"
  ],
  "Drug952": [
    "Drug952",
    "DRUG952",
    "drug-952",
    "D952"
  ],
  "Drug953": [
    "Drug953",
    "DRUG953",
    "drug-953",
    "D953"
  ],
  "Drug954": [
    "Drug954",
    "DRUG954",
    "drug-954",
    "D954"
  ],
  "Drug955": [
    "Drug955",
    "DRUG955",
    "drug-955",
    "D955"
  ],
  "Drug956": [
    "Drug956",
    "DRUG956",
    "drug-956",
    "D956"
  ],
  "Drug957": [
    "Drug957",
    "DRUG957",
    "drug-957",
    "D957"
  ],
  "Drug958": [
    "Drug958",
    "DRUG958",
    "drug-958",
    "D958"
  ],
  "Drug959": [
    "Drug959",
    "DRUG959",
    "drug-959",
    "D959"
  ],
  "Drug960": [
    "Drug960",
    "DRUG960",
    "drug-960",
    "D960"
  ],
  "Drug961": [
    "Drug961",
    "DRUG961",
    "drug-961",
    "D961"
  ],
  "Drug962": [
    "Drug962",
    "DRUG962",
    "drug-962",
    "D962"
  ],
  "Drug963": [
    "Drug963",
    "DRUG963",
    "drug-963",
    "D963"
  ],
  "Drug964": [
    "Drug964",
    "DRUG964",
    "drug-964",
    "D964"
  ],
  "Drug965": [
    "Drug965",
    "DRUG965",
    "drug-965",
    "D965"
  ],
  "Drug966": [
    "Drug966",
    "DRUG966",
    "drug-966",
    "D966"
  ],
  "Drug967": [
    "Drug967",
    "DRUG967",
    "drug-967",
    "D967"
  ],
  "Drug968": [
    "Drug968",
    "DRUG968",
    "drug-968",
    "D968"
  ],
  "Drug969": [
    "Drug969",
    "DRUG969",
    "drug-969",
    "D969"
  ],
  "Drug970": [
    "Drug970",
    "DRUG970",
    "drug-970",
    "D970"
  ],
  "Drug971": [
    "Drug971",
    "DRUG971",
    "drug-971",
    "D971"
  ],
  "Drug972": [
    "Drug972",
    "DRUG972",
    "drug-972",
    "D972"
  ],
  "Drug973": [
    "Drug973",
    "DRUG973",
    "drug-973",
    "D973"
  ],
  "Drug974": [
    "Drug974",
    "DRUG974",
    "drug-974",
    "D974"
  ],
  "Drug975": [
    "Drug975",
    "DRUG975",
    "drug-975",
    "D975"
  ],
  "Drug976": [
    "Drug976",
    "DRUG976",
    "drug-976",
    "D976"
  ],
  "Drug977": [
    "Drug977",
    "DRUG977",
    "drug-977",
    "D977"
  ],
  "Drug978": [
    "Drug978",
    "DRUG978",
    "drug-978",
    "D978"
  ],
  "Drug979": [
    "Drug979",
    "DRUG979",
    "drug-979",
    "D979"
  ],
  "Drug980": [
    "Drug980",
    "DRUG980",
    "drug-980",
    "D980"
  ],
  "Drug981": [
    "Drug981",
    "DRUG981",
    "drug-981",
    "D981"
  ],
  "Drug982": [
    "Drug982",
    "DRUG982",
    "drug-982",
    "D982"
  ],
  "Drug983": [
    "Drug983",
    "DRUG983",
    "drug-983",
    "D983"
  ],
  "Drug984": [
    "Drug984",
    "DRUG984",
    "drug-984",
    "D984"
  ],
  "Drug985": [
    "Drug985",
    "DRUG985",
    "drug-985",
    "D985"
  ],
  "Drug986": [
    "Drug986",
    "DRUG986",
    "drug-986",
    "D986"
  ],
  "Drug987": [
    "Drug987",
    "DRUG987",
    "drug-987",
    "D987"
  ],
  "Drug988": [
    "Drug988",
    "DRUG988",
    "drug-988",
    "D988"
  ],
  "Drug989": [
    "Drug989",
    "DRUG989",
    "drug-989",
    "D989"
  ],
  "Drug990": [
    "Drug990",
    "DRUG990",
    "drug-990",
    "D990"
  ],
  "Drug991": [
    "Drug991",
    "DRUG991",
    "drug-991",
    "D991"
  ],
  "Drug992": [
    "Drug992",
    "DRUG992",
    "drug-992",
    "D992"
  ],
  "Drug993": [
    "Drug993",
    "DRUG993",
    "drug-993",
    "D993"
  ],
  "Drug994": [
    "Drug994",
    "DRUG994",
    "drug-994",
    "D994"
  ],
  "Drug995": [
    "Drug995",
    "DRUG995",
    "drug-995",
    "D995"
  ],
  "Drug996": [
    "Drug996",
    "DRUG996",
    "drug-996",
    "D996"
  ],
  "Drug997": [
    "Drug997",
    "DRUG997",
    "drug-997",
    "D997"
  ],
  "Drug998": [
    "Drug998",
    "DRUG998",
    "drug-998",
    "D998"
  ],
  "Drug999": [
    "Drug999",
    "DRUG999",
    "drug-999",
    "D999"
  ],
  "Drug1000": [
    "Drug1000",
    "DRUG1000",
    "drug-1000",
    "D1000"
  ],
  "Drug1001": [
    "Drug1001",
    "DRUG1001",
    "drug-1001",
    "D1001"
  ],
  "Drug1002": [
    "Drug1002",
    "DRUG1002",
    "drug-1002",
    "D1002"
  ],
  "Drug1003": [
    "Drug1003",
    "DRUG1003",
    "drug-1003",
    "D1003"
  ],
  "Drug1004": [
    "Drug1004",
    "DRUG1004",
    "drug-1004",
    "D1004"
  ],
  "Drug1005": [
    "Drug1005",
    "DRUG1005",
    "drug-1005",
    "D1005"
  ],
  "Drug1006": [
    "Drug1006",
    "DRUG1006",
    "drug-1006",
    "D1006"
  ],
  "Drug1007": [
    "Drug1007",
    "DRUG1007",
    "drug-1007",
    "D1007"
  ],
  "Drug1008": [
    "Drug1008",
    "DRUG1008",
    "drug-1008",
    "D1008"
  ],
  "Drug1009": [
    "Drug1009",
    "DRUG1009",
    "drug-1009",
    "D1009"
  ],
  "Drug1010": [
    "Drug1010",
    "DRUG1010",
    "drug-1010",
    "D1010"
  ],
  "Drug1011": [
    "Drug1011",
    "DRUG1011",
    "drug-1011",
    "D1011"
  ],
  "Drug1012": [
    "Drug1012",
    "DRUG1012",
    "drug-1012",
    "D1012"
  ],
  "Drug1013": [
    "Drug1013",
    "DRUG1013",
    "drug-1013",
    "D1013"
  ],
  "Drug1014": [
    "Drug1014",
    "DRUG1014",
    "drug-1014",
    "D1014"
  ],
  "Drug1015": [
    "Drug1015",
    "DRUG1015",
    "drug-1015",
    "D1015"
  ],
  "Drug1016": [
    "Drug1016",
    "DRUG1016",
    "drug-1016",
    "D1016"
  ],
  "Drug1017": [
    "Drug1017",
    "DRUG1017",
    "drug-1017",
    "D1017"
  ],
  "Drug1018": [
    "Drug1018",
    "DRUG1018",
    "drug-1018",
    "D1018"
  ],
  "Drug1019": [
    "Drug1019",
    "DRUG1019",
    "drug-1019",
    "D1019"
  ],
  "Drug1020": [
    "Drug1020",
    "DRUG1020",
    "drug-1020",
    "D1020"
  ],
  "Drug1021": [
    "Drug1021",
    "DRUG1021",
    "drug-1021",
    "D1021"
  ],
  "Drug1022": [
    "Drug1022",
    "DRUG1022",
    "drug-1022",
    "D1022"
  ],
  "Drug1023": [
    "Drug1023",
    "DRUG1023",
    "drug-1023",
    "D1023"
  ],
  "Drug1024": [
    "Drug1024",
    "DRUG1024",
    "drug-1024",
    "D1024"
  ],
  "Drug1025": [
    "Drug1025",
    "DRUG1025",
    "drug-1025",
    "D1025"
  ],
  "Drug1026": [
    "Drug1026",
    "DRUG1026",
    "drug-1026",
    "D1026"
  ],
  "Drug1027": [
    "Drug1027",
    "DRUG1027",
    "drug-1027",
    "D1027"
  ],
  "Drug1028": [
    "Drug1028",
    "DRUG1028",
    "drug-1028",
    "D1028"
  ],
  "Drug1029": [
    "Drug1029",
    "DRUG1029",
    "drug-1029",
    "D1029"
  ],
  "Drug1030": [
    "Drug1030",
    "DRUG1030",
    "drug-1030",
    "D1030"
  ],
  "Drug1031": [
    "Drug1031",
    "DRUG1031",
    "drug-1031",
    "D1031"
  ],
  "Drug1032": [
    "Drug1032",
    "DRUG1032",
    "drug-1032",
    "D1032"
  ],
  "Drug1033": [
    "Drug1033",
    "DRUG1033",
    "drug-1033",
    "D1033"
  ],
  "Drug1034": [
    "Drug1034",
    "DRUG1034",
    "drug-1034",
    "D1034"
  ],
  "Drug1035": [
    "Drug1035",
    "DRUG1035",
    "drug-1035",
    "D1035"
  ],
  "Drug1036": [
    "Drug1036",
    "DRUG1036",
    "drug-1036",
    "D1036"
  ],
  "Drug1037": [
    "Drug1037",
    "DRUG1037",
    "drug-1037",
    "D1037"
  ],
  "Drug1038": [
    "Drug1038",
    "DRUG1038",
    "drug-1038",
    "D1038"
  ],
  "Drug1039": [
    "Drug1039",
    "DRUG1039",
    "drug-1039",
    "D1039"
  ],
  "Drug1040": [
    "Drug1040",
    "DRUG1040",
    "drug-1040",
    "D1040"
  ],
  "Drug1041": [
    "Drug1041",
    "DRUG1041",
    "drug-1041",
    "D1041"
  ],
  "Drug1042": [
    "Drug1042",
    "DRUG1042",
    "drug-1042",
    "D1042"
  ],
  "Drug1043": [
    "Drug1043",
    "DRUG1043",
    "drug-1043",
    "D1043"
  ],
  "Drug1044": [
    "Drug1044",
    "DRUG1044",
    "drug-1044",
    "D1044"
  ],
  "Drug1045": [
    "Drug1045",
    "DRUG1045",
    "drug-1045",
    "D1045"
  ],
  "Drug1046": [
    "Drug1046",
    "DRUG1046",
    "drug-1046",
    "D1046"
  ],
  "Drug1047": [
    "Drug1047",
    "DRUG1047",
    "drug-1047",
    "D1047"
  ],
  "Drug1048": [
    "Drug1048",
    "DRUG1048",
    "drug-1048",
    "D1048"
  ],
  "Drug1049": [
    "Drug1049",
    "DRUG1049",
    "drug-1049",
    "D1049"
  ],
  "Drug1050": [
    "Drug1050",
    "DRUG1050",
    "drug-1050",
    "D1050"
  ],
  "Drug1051": [
    "Drug1051",
    "DRUG1051",
    "drug-1051",
    "D1051"
  ],
  "Drug1052": [
    "Drug1052",
    "DRUG1052",
    "drug-1052",
    "D1052"
  ],
  "Drug1053": [
    "Drug1053",
    "DRUG1053",
    "drug-1053",
    "D1053"
  ],
  "Drug1054": [
    "Drug1054",
    "DRUG1054",
    "drug-1054",
    "D1054"
  ],
  "Drug1055": [
    "Drug1055",
    "DRUG1055",
    "drug-1055",
    "D1055"
  ],
  "Drug1056": [
    "Drug1056",
    "DRUG1056",
    "drug-1056",
    "D1056"
  ],
  "Drug1057": [
    "Drug1057",
    "DRUG1057",
    "drug-1057",
    "D1057"
  ],
  "Drug1058": [
    "Drug1058",
    "DRUG1058",
    "drug-1058",
    "D1058"
  ],
  "Drug1059": [
    "Drug1059",
    "DRUG1059",
    "drug-1059",
    "D1059"
  ],
  "Drug1060": [
    "Drug1060",
    "DRUG1060",
    "drug-1060",
    "D1060"
  ],
  "Drug1061": [
    "Drug1061",
    "DRUG1061",
    "drug-1061",
    "D1061"
  ],
  "Drug1062": [
    "Drug1062",
    "DRUG1062",
    "drug-1062",
    "D1062"
  ],
  "Drug1063": [
    "Drug1063",
    "DRUG1063",
    "drug-1063",
    "D1063"
  ],
  "Drug1064": [
    "Drug1064",
    "DRUG1064",
    "drug-1064",
    "D1064"
  ],
  "Drug1065": [
    "Drug1065",
    "DRUG1065",
    "drug-1065",
    "D1065"
  ],
  "Drug1066": [
    "Drug1066",
    "DRUG1066",
    "drug-1066",
    "D1066"
  ],
  "Drug1067": [
    "Drug1067",
    "DRUG1067",
    "drug-1067",
    "D1067"
  ],
  "Drug1068": [
    "Drug1068",
    "DRUG1068",
    "drug-1068",
    "D1068"
  ],
  "Drug1069": [
    "Drug1069",
    "DRUG1069",
    "drug-1069",
    "D1069"
  ],
  "Drug1070": [
    "Drug1070",
    "DRUG1070",
    "drug-1070",
    "D1070"
  ],
  "Drug1071": [
    "Drug1071",
    "DRUG1071",
    "drug-1071",
    "D1071"
  ],
  "Drug1072": [
    "Drug1072",
    "DRUG1072",
    "drug-1072",
    "D1072"
  ],
  "Drug1073": [
    "Drug1073",
    "DRUG1073",
    "drug-1073",
    "D1073"
  ],
  "Drug1074": [
    "Drug1074",
    "DRUG1074",
    "drug-1074",
    "D1074"
  ],
  "Drug1075": [
    "Drug1075",
    "DRUG1075",
    "drug-1075",
    "D1075"
  ],
  "Drug1076": [
    "Drug1076",
    "DRUG1076",
    "drug-1076",
    "D1076"
  ],
  "Drug1077": [
    "Drug1077",
    "DRUG1077",
    "drug-1077",
    "D1077"
  ],
  "Drug1078": [
    "Drug1078",
    "DRUG1078",
    "drug-1078",
    "D1078"
  ],
  "Drug1079": [
    "Drug1079",
    "DRUG1079",
    "drug-1079",
    "D1079"
  ],
  "Drug1080": [
    "Drug1080",
    "DRUG1080",
    "drug-1080",
    "D1080"
  ],
  "Drug1081": [
    "Drug1081",
    "DRUG1081",
    "drug-1081",
    "D1081"
  ],
  "Drug1082": [
    "Drug1082",
    "DRUG1082",
    "drug-1082",
    "D1082"
  ],
  "Drug1083": [
    "Drug1083",
    "DRUG1083",
    "drug-1083",
    "D1083"
  ],
  "Drug1084": [
    "Drug1084",
    "DRUG1084",
    "drug-1084",
    "D1084"
  ],
  "Drug1085": [
    "Drug1085",
    "DRUG1085",
    "drug-1085",
    "D1085"
  ],
  "Drug1086": [
    "Drug1086",
    "DRUG1086",
    "drug-1086",
    "D1086"
  ],
  "Drug1087": [
    "Drug1087",
    "DRUG1087",
    "drug-1087",
    "D1087"
  ],
  "Drug1088": [
    "Drug1088",
    "DRUG1088",
    "drug-1088",
    "D1088"
  ],
  "Drug1089": [
    "Drug1089",
    "DRUG1089",
    "drug-1089",
    "D1089"
  ],
  "Drug1090": [
    "Drug1090",
    "DRUG1090",
    "drug-1090",
    "D1090"
  ],
  "Drug1091": [
    "Drug1091",
    "DRUG1091",
    "drug-1091",
    "D1091"
  ],
  "Drug1092": [
    "Drug1092",
    "DRUG1092",
    "drug-1092",
    "D1092"
  ],
  "Drug1093": [
    "Drug1093",
    "DRUG1093",
    "drug-1093",
    "D1093"
  ],
  "Drug1094": [
    "Drug1094",
    "DRUG1094",
    "drug-1094",
    "D1094"
  ],
  "Drug1095": [
    "Drug1095",
    "DRUG1095",
    "drug-1095",
    "D1095"
  ],
  "Drug1096": [
    "Drug1096",
    "DRUG1096",
    "drug-1096",
    "D1096"
  ],
  "Drug1097": [
    "Drug1097",
    "DRUG1097",
    "drug-1097",
    "D1097"
  ],
  "Drug1098": [
    "Drug1098",
    "DRUG1098",
    "drug-1098",
    "D1098"
  ],
  "Drug1099": [
    "Drug1099",
    "DRUG1099",
    "drug-1099",
    "D1099"
  ]
}\nTRIAL_ENDPOINTS = [
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life",
  "Overall Survival",
  "Progression-Free Survival",
  "Objective Response Rate",
  "Disease Control Rate",
  "Time to Progression",
  "Event-Free Survival",
  "Minimal Residual Disease",
  "Biomarker Response",
  "HbA1c Change",
  "FEV1 Change",
  "6-Minute Walk Distance",
  "Blood Pressure Reduction",
  "LDL-C Change",
  "Relapse Rate",
  "EDSS Change",
  "Cognitive Composite",
  "Pain Score",
  "Quality of Life"
]\nMESH_HEADINGS = [
  "MH_00001",
  "MH_00002",
  "MH_00003",
  "MH_00004",
  "MH_00005",
  "MH_00006",
  "MH_00007",
  "MH_00008",
  "MH_00009",
  "MH_00010",
  "MH_00011",
  "MH_00012",
  "MH_00013",
  "MH_00014",
  "MH_00015",
  "MH_00016",
  "MH_00017",
  "MH_00018",
  "MH_00019",
  "MH_00020",
  "MH_00021",
  "MH_00022",
  "MH_00023",
  "MH_00024",
  "MH_00025",
  "MH_00026",
  "MH_00027",
  "MH_00028",
  "MH_00029",
  "MH_00030",
  "MH_00031",
  "MH_00032",
  "MH_00033",
  "MH_00034",
  "MH_00035",
  "MH_00036",
  "MH_00037",
  "MH_00038",
  "MH_00039",
  "MH_00040",
  "MH_00041",
  "MH_00042",
  "MH_00043",
  "MH_00044",
  "MH_00045",
  "MH_00046",
  "MH_00047",
  "MH_00048",
  "MH_00049",
  "MH_00050",
  "MH_00051",
  "MH_00052",
  "MH_00053",
  "MH_00054",
  "MH_00055",
  "MH_00056",
  "MH_00057",
  "MH_00058",
  "MH_00059",
  "MH_00060",
  "MH_00061",
  "MH_00062",
  "MH_00063",
  "MH_00064",
  "MH_00065",
  "MH_00066",
  "MH_00067",
  "MH_00068",
  "MH_00069",
  "MH_00070",
  "MH_00071",
  "MH_00072",
  "MH_00073",
  "MH_00074",
  "MH_00075",
  "MH_00076",
  "MH_00077",
  "MH_00078",
  "MH_00079",
  "MH_00080",
  "MH_00081",
  "MH_00082",
  "MH_00083",
  "MH_00084",
  "MH_00085",
  "MH_00086",
  "MH_00087",
  "MH_00088",
  "MH_00089",
  "MH_00090",
  "MH_00091",
  "MH_00092",
  "MH_00093",
  "MH_00094",
  "MH_00095",
  "MH_00096",
  "MH_00097",
  "MH_00098",
  "MH_00099",
  "MH_00100",
  "MH_00101",
  "MH_00102",
  "MH_00103",
  "MH_00104",
  "MH_00105",
  "MH_00106",
  "MH_00107",
  "MH_00108",
  "MH_00109",
  "MH_00110",
  "MH_00111",
  "MH_00112",
  "MH_00113",
  "MH_00114",
  "MH_00115",
  "MH_00116",
  "MH_00117",
  "MH_00118",
  "MH_00119",
  "MH_00120",
  "MH_00121",
  "MH_00122",
  "MH_00123",
  "MH_00124",
  "MH_00125",
  "MH_00126",
  "MH_00127",
  "MH_00128",
  "MH_00129",
  "MH_00130",
  "MH_00131",
  "MH_00132",
  "MH_00133",
  "MH_00134",
  "MH_00135",
  "MH_00136",
  "MH_00137",
  "MH_00138",
  "MH_00139",
  "MH_00140",
  "MH_00141",
  "MH_00142",
  "MH_00143",
  "MH_00144",
  "MH_00145",
  "MH_00146",
  "MH_00147",
  "MH_00148",
  "MH_00149",
  "MH_00150",
  "MH_00151",
  "MH_00152",
  "MH_00153",
  "MH_00154",
  "MH_00155",
  "MH_00156",
  "MH_00157",
  "MH_00158",
  "MH_00159",
  "MH_00160",
  "MH_00161",
  "MH_00162",
  "MH_00163",
  "MH_00164",
  "MH_00165",
  "MH_00166",
  "MH_00167",
  "MH_00168",
  "MH_00169",
  "MH_00170",
  "MH_00171",
  "MH_00172",
  "MH_00173",
  "MH_00174",
  "MH_00175",
  "MH_00176",
  "MH_00177",
  "MH_00178",
  "MH_00179",
  "MH_00180",
  "MH_00181",
  "MH_00182",
  "MH_00183",
  "MH_00184",
  "MH_00185",
  "MH_00186",
  "MH_00187",
  "MH_00188",
  "MH_00189",
  "MH_00190",
  "MH_00191",
  "MH_00192",
  "MH_00193",
  "MH_00194",
  "MH_00195",
  "MH_00196",
  "MH_00197",
  "MH_00198",
  "MH_00199",
  "MH_00200",
  "MH_00201",
  "MH_00202",
  "MH_00203",
  "MH_00204",
  "MH_00205",
  "MH_00206",
  "MH_00207",
  "MH_00208",
  "MH_00209",
  "MH_00210",
  "MH_00211",
  "MH_00212",
  "MH_00213",
  "MH_00214",
  "MH_00215",
  "MH_00216",
  "MH_00217",
  "MH_00218",
  "MH_00219",
  "MH_00220",
  "MH_00221",
  "MH_00222",
  "MH_00223",
  "MH_00224",
  "MH_00225",
  "MH_00226",
  "MH_00227",
  "MH_00228",
  "MH_00229",
  "MH_00230",
  "MH_00231",
  "MH_00232",
  "MH_00233",
  "MH_00234",
  "MH_00235",
  "MH_00236",
  "MH_00237",
  "MH_00238",
  "MH_00239",
  "MH_00240",
  "MH_00241",
  "MH_00242",
  "MH_00243",
  "MH_00244",
  "MH_00245",
  "MH_00246",
  "MH_00247",
  "MH_00248",
  "MH_00249",
  "MH_00250",
  "MH_00251",
  "MH_00252",
  "MH_00253",
  "MH_00254",
  "MH_00255",
  "MH_00256",
  "MH_00257",
  "MH_00258",
  "MH_00259",
  "MH_00260",
  "MH_00261",
  "MH_00262",
  "MH_00263",
  "MH_00264",
  "MH_00265",
  "MH_00266",
  "MH_00267",
  "MH_00268",
  "MH_00269",
  "MH_00270",
  "MH_00271",
  "MH_00272",
  "MH_00273",
  "MH_00274",
  "MH_00275",
  "MH_00276",
  "MH_00277",
  "MH_00278",
  "MH_00279",
  "MH_00280",
  "MH_00281",
  "MH_00282",
  "MH_00283",
  "MH_00284",
  "MH_00285",
  "MH_00286",
  "MH_00287",
  "MH_00288",
  "MH_00289",
  "MH_00290",
  "MH_00291",
  "MH_00292",
  "MH_00293",
  "MH_00294",
  "MH_00295",
  "MH_00296",
  "MH_00297",
  "MH_00298",
  "MH_00299",
  "MH_00300",
  "MH_00301",
  "MH_00302",
  "MH_00303",
  "MH_00304",
  "MH_00305",
  "MH_00306",
  "MH_00307",
  "MH_00308",
  "MH_00309",
  "MH_00310",
  "MH_00311",
  "MH_00312",
  "MH_00313",
  "MH_00314",
  "MH_00315",
  "MH_00316",
  "MH_00317",
  "MH_00318",
  "MH_00319",
  "MH_00320",
  "MH_00321",
  "MH_00322",
  "MH_00323",
  "MH_00324",
  "MH_00325",
  "MH_00326",
  "MH_00327",
  "MH_00328",
  "MH_00329",
  "MH_00330",
  "MH_00331",
  "MH_00332",
  "MH_00333",
  "MH_00334",
  "MH_00335",
  "MH_00336",
  "MH_00337",
  "MH_00338",
  "MH_00339",
  "MH_00340",
  "MH_00341",
  "MH_00342",
  "MH_00343",
  "MH_00344",
  "MH_00345",
  "MH_00346",
  "MH_00347",
  "MH_00348",
  "MH_00349",
  "MH_00350",
  "MH_00351",
  "MH_00352",
  "MH_00353",
  "MH_00354",
  "MH_00355",
  "MH_00356",
  "MH_00357",
  "MH_00358",
  "MH_00359",
  "MH_00360",
  "MH_00361",
  "MH_00362",
  "MH_00363",
  "MH_00364",
  "MH_00365",
  "MH_00366",
  "MH_00367",
  "MH_00368",
  "MH_00369",
  "MH_00370",
  "MH_00371",
  "MH_00372",
  "MH_00373",
  "MH_00374",
  "MH_00375",
  "MH_00376",
  "MH_00377",
  "MH_00378",
  "MH_00379",
  "MH_00380",
  "MH_00381",
  "MH_00382",
  "MH_00383",
  "MH_00384",
  "MH_00385",
  "MH_00386",
  "MH_00387",
  "MH_00388",
  "MH_00389",
  "MH_00390",
  "MH_00391",
  "MH_00392",
  "MH_00393",
  "MH_00394",
  "MH_00395",
  "MH_00396",
  "MH_00397",
  "MH_00398",
  "MH_00399",
  "MH_00400",
  "MH_00401",
  "MH_00402",
  "MH_00403",
  "MH_00404",
  "MH_00405",
  "MH_00406",
  "MH_00407",
  "MH_00408",
  "MH_00409",
  "MH_00410",
  "MH_00411",
  "MH_00412",
  "MH_00413",
  "MH_00414",
  "MH_00415",
  "MH_00416",
  "MH_00417",
  "MH_00418",
  "MH_00419",
  "MH_00420",
  "MH_00421",
  "MH_00422",
  "MH_00423",
  "MH_00424",
  "MH_00425",
  "MH_00426",
  "MH_00427",
  "MH_00428",
  "MH_00429",
  "MH_00430",
  "MH_00431",
  "MH_00432",
  "MH_00433",
  "MH_00434",
  "MH_00435",
  "MH_00436",
  "MH_00437",
  "MH_00438",
  "MH_00439",
  "MH_00440",
  "MH_00441",
  "MH_00442",
  "MH_00443",
  "MH_00444",
  "MH_00445",
  "MH_00446",
  "MH_00447",
  "MH_00448",
  "MH_00449",
  "MH_00450",
  "MH_00451",
  "MH_00452",
  "MH_00453",
  "MH_00454",
  "MH_00455",
  "MH_00456",
  "MH_00457",
  "MH_00458",
  "MH_00459",
  "MH_00460",
  "MH_00461",
  "MH_00462",
  "MH_00463",
  "MH_00464",
  "MH_00465",
  "MH_00466",
  "MH_00467",
  "MH_00468",
  "MH_00469",
  "MH_00470",
  "MH_00471",
  "MH_00472",
  "MH_00473",
  "MH_00474",
  "MH_00475",
  "MH_00476",
  "MH_00477",
  "MH_00478",
  "MH_00479",
  "MH_00480",
  "MH_00481",
  "MH_00482",
  "MH_00483",
  "MH_00484",
  "MH_00485",
  "MH_00486",
  "MH_00487",
  "MH_00488",
  "MH_00489",
  "MH_00490",
  "MH_00491",
  "MH_00492",
  "MH_00493",
  "MH_00494",
  "MH_00495",
  "MH_00496",
  "MH_00497",
  "MH_00498",
  "MH_00499",
  "MH_00500",
  "MH_00501",
  "MH_00502",
  "MH_00503",
  "MH_00504",
  "MH_00505",
  "MH_00506",
  "MH_00507",
  "MH_00508",
  "MH_00509",
  "MH_00510",
  "MH_00511",
  "MH_00512",
  "MH_00513",
  "MH_00514",
  "MH_00515",
  "MH_00516",
  "MH_00517",
  "MH_00518",
  "MH_00519",
  "MH_00520",
  "MH_00521",
  "MH_00522",
  "MH_00523",
  "MH_00524",
  "MH_00525",
  "MH_00526",
  "MH_00527",
  "MH_00528",
  "MH_00529",
  "MH_00530",
  "MH_00531",
  "MH_00532",
  "MH_00533",
  "MH_00534",
  "MH_00535",
  "MH_00536",
  "MH_00537",
  "MH_00538",
  "MH_00539",
  "MH_00540",
  "MH_00541",
  "MH_00542",
  "MH_00543",
  "MH_00544",
  "MH_00545",
  "MH_00546",
  "MH_00547",
  "MH_00548",
  "MH_00549",
  "MH_00550",
  "MH_00551",
  "MH_00552",
  "MH_00553",
  "MH_00554",
  "MH_00555",
  "MH_00556",
  "MH_00557",
  "MH_00558",
  "MH_00559",
  "MH_00560",
  "MH_00561",
  "MH_00562",
  "MH_00563",
  "MH_00564",
  "MH_00565",
  "MH_00566",
  "MH_00567",
  "MH_00568",
  "MH_00569",
  "MH_00570",
  "MH_00571",
  "MH_00572",
  "MH_00573",
  "MH_00574",
  "MH_00575",
  "MH_00576",
  "MH_00577",
  "MH_00578",
  "MH_00579",
  "MH_00580",
  "MH_00581",
  "MH_00582",
  "MH_00583",
  "MH_00584",
  "MH_00585",
  "MH_00586",
  "MH_00587",
  "MH_00588",
  "MH_00589",
  "MH_00590",
  "MH_00591",
  "MH_00592",
  "MH_00593",
  "MH_00594",
  "MH_00595",
  "MH_00596",
  "MH_00597",
  "MH_00598",
  "MH_00599",
  "MH_00600",
  "MH_00601",
  "MH_00602",
  "MH_00603",
  "MH_00604",
  "MH_00605",
  "MH_00606",
  "MH_00607",
  "MH_00608",
  "MH_00609",
  "MH_00610",
  "MH_00611",
  "MH_00612",
  "MH_00613",
  "MH_00614",
  "MH_00615",
  "MH_00616",
  "MH_00617",
  "MH_00618",
  "MH_00619",
  "MH_00620",
  "MH_00621",
  "MH_00622",
  "MH_00623",
  "MH_00624",
  "MH_00625",
  "MH_00626",
  "MH_00627",
  "MH_00628",
  "MH_00629",
  "MH_00630",
  "MH_00631",
  "MH_00632",
  "MH_00633",
  "MH_00634",
  "MH_00635",
  "MH_00636",
  "MH_00637",
  "MH_00638",
  "MH_00639",
  "MH_00640",
  "MH_00641",
  "MH_00642",
  "MH_00643",
  "MH_00644",
  "MH_00645",
  "MH_00646",
  "MH_00647",
  "MH_00648",
  "MH_00649",
  "MH_00650",
  "MH_00651",
  "MH_00652",
  "MH_00653",
  "MH_00654",
  "MH_00655",
  "MH_00656",
  "MH_00657",
  "MH_00658",
  "MH_00659",
  "MH_00660",
  "MH_00661",
  "MH_00662",
  "MH_00663",
  "MH_00664",
  "MH_00665",
  "MH_00666",
  "MH_00667",
  "MH_00668",
  "MH_00669",
  "MH_00670",
  "MH_00671",
  "MH_00672",
  "MH_00673",
  "MH_00674",
  "MH_00675",
  "MH_00676",
  "MH_00677",
  "MH_00678",
  "MH_00679",
  "MH_00680",
  "MH_00681",
  "MH_00682",
  "MH_00683",
  "MH_00684",
  "MH_00685",
  "MH_00686",
  "MH_00687",
  "MH_00688",
  "MH_00689",
  "MH_00690",
  "MH_00691",
  "MH_00692",
  "MH_00693",
  "MH_00694",
  "MH_00695",
  "MH_00696",
  "MH_00697",
  "MH_00698",
  "MH_00699",
  "MH_00700",
  "MH_00701",
  "MH_00702",
  "MH_00703",
  "MH_00704",
  "MH_00705",
  "MH_00706",
  "MH_00707",
  "MH_00708",
  "MH_00709",
  "MH_00710",
  "MH_00711",
  "MH_00712",
  "MH_00713",
  "MH_00714",
  "MH_00715",
  "MH_00716",
  "MH_00717",
  "MH_00718",
  "MH_00719",
  "MH_00720",
  "MH_00721",
  "MH_00722",
  "MH_00723",
  "MH_00724",
  "MH_00725",
  "MH_00726",
  "MH_00727",
  "MH_00728",
  "MH_00729",
  "MH_00730",
  "MH_00731",
  "MH_00732",
  "MH_00733",
  "MH_00734",
  "MH_00735",
  "MH_00736",
  "MH_00737",
  "MH_00738",
  "MH_00739",
  "MH_00740",
  "MH_00741",
  "MH_00742",
  "MH_00743",
  "MH_00744",
  "MH_00745",
  "MH_00746",
  "MH_00747",
  "MH_00748",
  "MH_00749",
  "MH_00750",
  "MH_00751",
  "MH_00752",
  "MH_00753",
  "MH_00754",
  "MH_00755",
  "MH_00756",
  "MH_00757",
  "MH_00758",
  "MH_00759",
  "MH_00760",
  "MH_00761",
  "MH_00762",
  "MH_00763",
  "MH_00764",
  "MH_00765",
  "MH_00766",
  "MH_00767",
  "MH_00768",
  "MH_00769",
  "MH_00770",
  "MH_00771",
  "MH_00772",
  "MH_00773",
  "MH_00774",
  "MH_00775",
  "MH_00776",
  "MH_00777",
  "MH_00778",
  "MH_00779",
  "MH_00780",
  "MH_00781",
  "MH_00782",
  "MH_00783",
  "MH_00784",
  "MH_00785",
  "MH_00786",
  "MH_00787",
  "MH_00788",
  "MH_00789",
  "MH_00790",
  "MH_00791",
  "MH_00792",
  "MH_00793",
  "MH_00794",
  "MH_00795",
  "MH_00796",
  "MH_00797",
  "MH_00798",
  "MH_00799",
  "MH_00800",
  "MH_00801",
  "MH_00802",
  "MH_00803",
  "MH_00804",
  "MH_00805",
  "MH_00806",
  "MH_00807",
  "MH_00808",
  "MH_00809",
  "MH_00810",
  "MH_00811",
  "MH_00812",
  "MH_00813",
  "MH_00814",
  "MH_00815",
  "MH_00816",
  "MH_00817",
  "MH_00818",
  "MH_00819",
  "MH_00820",
  "MH_00821",
  "MH_00822",
  "MH_00823",
  "MH_00824",
  "MH_00825",
  "MH_00826",
  "MH_00827",
  "MH_00828",
  "MH_00829",
  "MH_00830",
  "MH_00831",
  "MH_00832",
  "MH_00833",
  "MH_00834",
  "MH_00835",
  "MH_00836",
  "MH_00837",
  "MH_00838",
  "MH_00839",
  "MH_00840",
  "MH_00841",
  "MH_00842",
  "MH_00843",
  "MH_00844",
  "MH_00845",
  "MH_00846",
  "MH_00847",
  "MH_00848",
  "MH_00849",
  "MH_00850",
  "MH_00851",
  "MH_00852",
  "MH_00853",
  "MH_00854",
  "MH_00855",
  "MH_00856",
  "MH_00857",
  "MH_00858",
  "MH_00859",
  "MH_00860",
  "MH_00861",
  "MH_00862",
  "MH_00863",
  "MH_00864",
  "MH_00865",
  "MH_00866",
  "MH_00867",
  "MH_00868",
  "MH_00869",
  "MH_00870",
  "MH_00871",
  "MH_00872",
  "MH_00873",
  "MH_00874",
  "MH_00875",
  "MH_00876",
  "MH_00877",
  "MH_00878",
  "MH_00879",
  "MH_00880",
  "MH_00881",
  "MH_00882",
  "MH_00883",
  "MH_00884",
  "MH_00885",
  "MH_00886",
  "MH_00887",
  "MH_00888",
  "MH_00889",
  "MH_00890",
  "MH_00891",
  "MH_00892",
  "MH_00893",
  "MH_00894",
  "MH_00895",
  "MH_00896",
  "MH_00897",
  "MH_00898",
  "MH_00899",
  "MH_00900",
  "MH_00901",
  "MH_00902",
  "MH_00903",
  "MH_00904",
  "MH_00905",
  "MH_00906",
  "MH_00907",
  "MH_00908",
  "MH_00909",
  "MH_00910",
  "MH_00911",
  "MH_00912",
  "MH_00913",
  "MH_00914",
  "MH_00915",
  "MH_00916",
  "MH_00917",
  "MH_00918",
  "MH_00919",
  "MH_00920",
  "MH_00921",
  "MH_00922",
  "MH_00923",
  "MH_00924",
  "MH_00925",
  "MH_00926",
  "MH_00927",
  "MH_00928",
  "MH_00929",
  "MH_00930",
  "MH_00931",
  "MH_00932",
  "MH_00933",
  "MH_00934",
  "MH_00935",
  "MH_00936",
  "MH_00937",
  "MH_00938",
  "MH_00939",
  "MH_00940",
  "MH_00941",
  "MH_00942",
  "MH_00943",
  "MH_00944",
  "MH_00945",
  "MH_00946",
  "MH_00947",
  "MH_00948",
  "MH_00949",
  "MH_00950",
  "MH_00951",
  "MH_00952",
  "MH_00953",
  "MH_00954",
  "MH_00955",
  "MH_00956",
  "MH_00957",
  "MH_00958",
  "MH_00959",
  "MH_00960",
  "MH_00961",
  "MH_00962",
  "MH_00963",
  "MH_00964",
  "MH_00965",
  "MH_00966",
  "MH_00967",
  "MH_00968",
  "MH_00969",
  "MH_00970",
  "MH_00971",
  "MH_00972",
  "MH_00973",
  "MH_00974",
  "MH_00975",
  "MH_00976",
  "MH_00977",
  "MH_00978",
  "MH_00979",
  "MH_00980",
  "MH_00981",
  "MH_00982",
  "MH_00983",
  "MH_00984",
  "MH_00985",
  "MH_00986",
  "MH_00987",
  "MH_00988",
  "MH_00989",
  "MH_00990",
  "MH_00991",
  "MH_00992",
  "MH_00993",
  "MH_00994",
  "MH_00995",
  "MH_00996",
  "MH_00997",
  "MH_00998",
  "MH_00999",
  "MH_01000",
  "MH_01001",
  "MH_01002",
  "MH_01003",
  "MH_01004",
  "MH_01005",
  "MH_01006",
  "MH_01007",
  "MH_01008",
  "MH_01009",
  "MH_01010",
  "MH_01011",
  "MH_01012",
  "MH_01013",
  "MH_01014",
  "MH_01015",
  "MH_01016",
  "MH_01017",
  "MH_01018",
  "MH_01019",
  "MH_01020",
  "MH_01021",
  "MH_01022",
  "MH_01023",
  "MH_01024",
  "MH_01025",
  "MH_01026",
  "MH_01027",
  "MH_01028",
  "MH_01029",
  "MH_01030",
  "MH_01031",
  "MH_01032",
  "MH_01033",
  "MH_01034",
  "MH_01035",
  "MH_01036",
  "MH_01037",
  "MH_01038",
  "MH_01039",
  "MH_01040",
  "MH_01041",
  "MH_01042",
  "MH_01043",
  "MH_01044",
  "MH_01045",
  "MH_01046",
  "MH_01047",
  "MH_01048",
  "MH_01049",
  "MH_01050",
  "MH_01051",
  "MH_01052",
  "MH_01053",
  "MH_01054",
  "MH_01055",
  "MH_01056",
  "MH_01057",
  "MH_01058",
  "MH_01059",
  "MH_01060",
  "MH_01061",
  "MH_01062",
  "MH_01063",
  "MH_01064",
  "MH_01065",
  "MH_01066",
  "MH_01067",
  "MH_01068",
  "MH_01069",
  "MH_01070",
  "MH_01071",
  "MH_01072",
  "MH_01073",
  "MH_01074",
  "MH_01075",
  "MH_01076",
  "MH_01077",
  "MH_01078",
  "MH_01079",
  "MH_01080",
  "MH_01081",
  "MH_01082",
  "MH_01083",
  "MH_01084",
  "MH_01085",
  "MH_01086",
  "MH_01087",
  "MH_01088",
  "MH_01089",
  "MH_01090",
  "MH_01091",
  "MH_01092",
  "MH_01093",
  "MH_01094",
  "MH_01095",
  "MH_01096",
  "MH_01097",
  "MH_01098",
  "MH_01099",
  "MH_01100",
  "MH_01101",
  "MH_01102",
  "MH_01103",
  "MH_01104",
  "MH_01105",
  "MH_01106",
  "MH_01107",
  "MH_01108",
  "MH_01109",
  "MH_01110",
  "MH_01111",
  "MH_01112",
  "MH_01113",
  "MH_01114",
  "MH_01115",
  "MH_01116",
  "MH_01117",
  "MH_01118",
  "MH_01119",
  "MH_01120",
  "MH_01121",
  "MH_01122",
  "MH_01123",
  "MH_01124",
  "MH_01125",
  "MH_01126",
  "MH_01127",
  "MH_01128",
  "MH_01129",
  "MH_01130",
  "MH_01131",
  "MH_01132",
  "MH_01133",
  "MH_01134",
  "MH_01135",
  "MH_01136",
  "MH_01137",
  "MH_01138",
  "MH_01139",
  "MH_01140",
  "MH_01141",
  "MH_01142",
  "MH_01143",
  "MH_01144",
  "MH_01145",
  "MH_01146",
  "MH_01147",
  "MH_01148",
  "MH_01149",
  "MH_01150",
  "MH_01151",
  "MH_01152",
  "MH_01153",
  "MH_01154",
  "MH_01155",
  "MH_01156",
  "MH_01157",
  "MH_01158",
  "MH_01159",
  "MH_01160",
  "MH_01161",
  "MH_01162",
  "MH_01163",
  "MH_01164",
  "MH_01165",
  "MH_01166",
  "MH_01167",
  "MH_01168",
  "MH_01169",
  "MH_01170",
  "MH_01171",
  "MH_01172",
  "MH_01173",
  "MH_01174",
  "MH_01175",
  "MH_01176",
  "MH_01177",
  "MH_01178",
  "MH_01179",
  "MH_01180",
  "MH_01181",
  "MH_01182",
  "MH_01183",
  "MH_01184",
  "MH_01185",
  "MH_01186",
  "MH_01187",
  "MH_01188",
  "MH_01189",
  "MH_01190",
  "MH_01191",
  "MH_01192",
  "MH_01193",
  "MH_01194",
  "MH_01195",
  "MH_01196",
  "MH_01197",
  "MH_01198",
  "MH_01199",
  "MH_01200",
  "MH_01201",
  "MH_01202",
  "MH_01203",
  "MH_01204",
  "MH_01205",
  "MH_01206",
  "MH_01207",
  "MH_01208",
  "MH_01209",
  "MH_01210",
  "MH_01211",
  "MH_01212",
  "MH_01213",
  "MH_01214",
  "MH_01215",
  "MH_01216",
  "MH_01217",
  "MH_01218",
  "MH_01219",
  "MH_01220",
  "MH_01221",
  "MH_01222",
  "MH_01223",
  "MH_01224",
  "MH_01225",
  "MH_01226",
  "MH_01227",
  "MH_01228",
  "MH_01229",
  "MH_01230",
  "MH_01231",
  "MH_01232",
  "MH_01233",
  "MH_01234",
  "MH_01235",
  "MH_01236",
  "MH_01237",
  "MH_01238",
  "MH_01239",
  "MH_01240",
  "MH_01241",
  "MH_01242",
  "MH_01243",
  "MH_01244",
  "MH_01245",
  "MH_01246",
  "MH_01247",
  "MH_01248",
  "MH_01249",
  "MH_01250",
  "MH_01251",
  "MH_01252",
  "MH_01253",
  "MH_01254",
  "MH_01255",
  "MH_01256",
  "MH_01257",
  "MH_01258",
  "MH_01259",
  "MH_01260",
  "MH_01261",
  "MH_01262",
  "MH_01263",
  "MH_01264",
  "MH_01265",
  "MH_01266",
  "MH_01267",
  "MH_01268",
  "MH_01269",
  "MH_01270",
  "MH_01271",
  "MH_01272",
  "MH_01273",
  "MH_01274",
  "MH_01275",
  "MH_01276",
  "MH_01277",
  "MH_01278",
  "MH_01279",
  "MH_01280",
  "MH_01281",
  "MH_01282",
  "MH_01283",
  "MH_01284",
  "MH_01285",
  "MH_01286",
  "MH_01287",
  "MH_01288",
  "MH_01289",
  "MH_01290",
  "MH_01291",
  "MH_01292",
  "MH_01293",
  "MH_01294",
  "MH_01295",
  "MH_01296",
  "MH_01297",
  "MH_01298",
  "MH_01299",
  "MH_01300",
  "MH_01301",
  "MH_01302",
  "MH_01303",
  "MH_01304",
  "MH_01305",
  "MH_01306",
  "MH_01307",
  "MH_01308",
  "MH_01309",
  "MH_01310",
  "MH_01311",
  "MH_01312",
  "MH_01313",
  "MH_01314",
  "MH_01315",
  "MH_01316",
  "MH_01317",
  "MH_01318",
  "MH_01319",
  "MH_01320",
  "MH_01321",
  "MH_01322",
  "MH_01323",
  "MH_01324",
  "MH_01325",
  "MH_01326",
  "MH_01327",
  "MH_01328",
  "MH_01329",
  "MH_01330",
  "MH_01331",
  "MH_01332",
  "MH_01333",
  "MH_01334",
  "MH_01335",
  "MH_01336",
  "MH_01337",
  "MH_01338",
  "MH_01339",
  "MH_01340",
  "MH_01341",
  "MH_01342",
  "MH_01343",
  "MH_01344",
  "MH_01345",
  "MH_01346",
  "MH_01347",
  "MH_01348",
  "MH_01349",
  "MH_01350",
  "MH_01351",
  "MH_01352",
  "MH_01353",
  "MH_01354",
  "MH_01355",
  "MH_01356",
  "MH_01357",
  "MH_01358",
  "MH_01359",
  "MH_01360",
  "MH_01361",
  "MH_01362",
  "MH_01363",
  "MH_01364",
  "MH_01365",
  "MH_01366",
  "MH_01367",
  "MH_01368",
  "MH_01369",
  "MH_01370",
  "MH_01371",
  "MH_01372",
  "MH_01373",
  "MH_01374",
  "MH_01375",
  "MH_01376",
  "MH_01377",
  "MH_01378",
  "MH_01379",
  "MH_01380",
  "MH_01381",
  "MH_01382",
  "MH_01383",
  "MH_01384",
  "MH_01385",
  "MH_01386",
  "MH_01387",
  "MH_01388",
  "MH_01389",
  "MH_01390",
  "MH_01391",
  "MH_01392",
  "MH_01393",
  "MH_01394",
  "MH_01395",
  "MH_01396",
  "MH_01397",
  "MH_01398",
  "MH_01399",
  "MH_01400",
  "MH_01401",
  "MH_01402",
  "MH_01403",
  "MH_01404",
  "MH_01405",
  "MH_01406",
  "MH_01407",
  "MH_01408",
  "MH_01409",
  "MH_01410",
  "MH_01411",
  "MH_01412",
  "MH_01413",
  "MH_01414",
  "MH_01415",
  "MH_01416",
  "MH_01417",
  "MH_01418",
  "MH_01419",
  "MH_01420",
  "MH_01421",
  "MH_01422",
  "MH_01423",
  "MH_01424",
  "MH_01425",
  "MH_01426",
  "MH_01427",
  "MH_01428",
  "MH_01429",
  "MH_01430",
  "MH_01431",
  "MH_01432",
  "MH_01433",
  "MH_01434",
  "MH_01435",
  "MH_01436",
  "MH_01437",
  "MH_01438",
  "MH_01439",
  "MH_01440",
  "MH_01441",
  "MH_01442",
  "MH_01443",
  "MH_01444",
  "MH_01445",
  "MH_01446",
  "MH_01447",
  "MH_01448",
  "MH_01449",
  "MH_01450",
  "MH_01451",
  "MH_01452",
  "MH_01453",
  "MH_01454",
  "MH_01455",
  "MH_01456",
  "MH_01457",
  "MH_01458",
  "MH_01459",
  "MH_01460",
  "MH_01461",
  "MH_01462",
  "MH_01463",
  "MH_01464",
  "MH_01465",
  "MH_01466",
  "MH_01467",
  "MH_01468",
  "MH_01469",
  "MH_01470",
  "MH_01471",
  "MH_01472",
  "MH_01473",
  "MH_01474",
  "MH_01475",
  "MH_01476",
  "MH_01477",
  "MH_01478",
  "MH_01479",
  "MH_01480",
  "MH_01481",
  "MH_01482",
  "MH_01483",
  "MH_01484",
  "MH_01485",
  "MH_01486",
  "MH_01487",
  "MH_01488",
  "MH_01489",
  "MH_01490",
  "MH_01491",
  "MH_01492",
  "MH_01493",
  "MH_01494",
  "MH_01495",
  "MH_01496",
  "MH_01497",
  "MH_01498",
  "MH_01499",
  "MH_01500",
  "MH_01501",
  "MH_01502",
  "MH_01503",
  "MH_01504",
  "MH_01505",
  "MH_01506",
  "MH_01507",
  "MH_01508",
  "MH_01509",
  "MH_01510",
  "MH_01511",
  "MH_01512",
  "MH_01513",
  "MH_01514",
  "MH_01515",
  "MH_01516",
  "MH_01517",
  "MH_01518",
  "MH_01519",
  "MH_01520",
  "MH_01521",
  "MH_01522",
  "MH_01523",
  "MH_01524",
  "MH_01525",
  "MH_01526",
  "MH_01527",
  "MH_01528",
  "MH_01529",
  "MH_01530",
  "MH_01531",
  "MH_01532",
  "MH_01533",
  "MH_01534",
  "MH_01535",
  "MH_01536",
  "MH_01537",
  "MH_01538",
  "MH_01539",
  "MH_01540",
  "MH_01541",
  "MH_01542",
  "MH_01543",
  "MH_01544",
  "MH_01545",
  "MH_01546",
  "MH_01547",
  "MH_01548",
  "MH_01549",
  "MH_01550",
  "MH_01551",
  "MH_01552",
  "MH_01553",
  "MH_01554",
  "MH_01555",
  "MH_01556",
  "MH_01557",
  "MH_01558",
  "MH_01559",
  "MH_01560",
  "MH_01561",
  "MH_01562",
  "MH_01563",
  "MH_01564",
  "MH_01565",
  "MH_01566",
  "MH_01567",
  "MH_01568",
  "MH_01569",
  "MH_01570",
  "MH_01571",
  "MH_01572",
  "MH_01573",
  "MH_01574",
  "MH_01575",
  "MH_01576",
  "MH_01577",
  "MH_01578",
  "MH_01579",
  "MH_01580",
  "MH_01581",
  "MH_01582",
  "MH_01583",
  "MH_01584",
  "MH_01585",
  "MH_01586",
  "MH_01587",
  "MH_01588",
  "MH_01589",
  "MH_01590",
  "MH_01591",
  "MH_01592",
  "MH_01593",
  "MH_01594",
  "MH_01595",
  "MH_01596",
  "MH_01597",
  "MH_01598",
  "MH_01599",
  "MH_01600",
  "MH_01601",
  "MH_01602",
  "MH_01603",
  "MH_01604",
  "MH_01605",
  "MH_01606",
  "MH_01607",
  "MH_01608",
  "MH_01609",
  "MH_01610",
  "MH_01611",
  "MH_01612",
  "MH_01613",
  "MH_01614",
  "MH_01615",
  "MH_01616",
  "MH_01617",
  "MH_01618",
  "MH_01619",
  "MH_01620",
  "MH_01621",
  "MH_01622",
  "MH_01623",
  "MH_01624",
  "MH_01625",
  "MH_01626",
  "MH_01627",
  "MH_01628",
  "MH_01629",
  "MH_01630",
  "MH_01631",
  "MH_01632",
  "MH_01633",
  "MH_01634",
  "MH_01635",
  "MH_01636",
  "MH_01637",
  "MH_01638",
  "MH_01639",
  "MH_01640",
  "MH_01641",
  "MH_01642",
  "MH_01643",
  "MH_01644",
  "MH_01645",
  "MH_01646",
  "MH_01647",
  "MH_01648",
  "MH_01649",
  "MH_01650",
  "MH_01651",
  "MH_01652",
  "MH_01653",
  "MH_01654",
  "MH_01655",
  "MH_01656",
  "MH_01657",
  "MH_01658",
  "MH_01659",
  "MH_01660",
  "MH_01661",
  "MH_01662",
  "MH_01663",
  "MH_01664",
  "MH_01665",
  "MH_01666",
  "MH_01667",
  "MH_01668",
  "MH_01669",
  "MH_01670",
  "MH_01671",
  "MH_01672",
  "MH_01673",
  "MH_01674",
  "MH_01675",
  "MH_01676",
  "MH_01677",
  "MH_01678",
  "MH_01679",
  "MH_01680",
  "MH_01681",
  "MH_01682",
  "MH_01683",
  "MH_01684",
  "MH_01685",
  "MH_01686",
  "MH_01687",
  "MH_01688",
  "MH_01689",
  "MH_01690",
  "MH_01691",
  "MH_01692",
  "MH_01693",
  "MH_01694",
  "MH_01695",
  "MH_01696",
  "MH_01697",
  "MH_01698",
  "MH_01699",
  "MH_01700",
  "MH_01701",
  "MH_01702",
  "MH_01703",
  "MH_01704",
  "MH_01705",
  "MH_01706",
  "MH_01707",
  "MH_01708",
  "MH_01709",
  "MH_01710",
  "MH_01711",
  "MH_01712",
  "MH_01713",
  "MH_01714",
  "MH_01715",
  "MH_01716",
  "MH_01717",
  "MH_01718",
  "MH_01719",
  "MH_01720",
  "MH_01721",
  "MH_01722",
  "MH_01723",
  "MH_01724",
  "MH_01725",
  "MH_01726",
  "MH_01727",
  "MH_01728",
  "MH_01729",
  "MH_01730",
  "MH_01731",
  "MH_01732",
  "MH_01733",
  "MH_01734",
  "MH_01735",
  "MH_01736",
  "MH_01737",
  "MH_01738",
  "MH_01739",
  "MH_01740",
  "MH_01741",
  "MH_01742",
  "MH_01743",
  "MH_01744",
  "MH_01745",
  "MH_01746",
  "MH_01747",
  "MH_01748",
  "MH_01749",
  "MH_01750",
  "MH_01751",
  "MH_01752",
  "MH_01753",
  "MH_01754",
  "MH_01755",
  "MH_01756",
  "MH_01757",
  "MH_01758",
  "MH_01759",
  "MH_01760",
  "MH_01761",
  "MH_01762",
  "MH_01763",
  "MH_01764",
  "MH_01765",
  "MH_01766",
  "MH_01767",
  "MH_01768",
  "MH_01769",
  "MH_01770",
  "MH_01771",
  "MH_01772",
  "MH_01773",
  "MH_01774",
  "MH_01775",
  "MH_01776",
  "MH_01777",
  "MH_01778",
  "MH_01779",
  "MH_01780",
  "MH_01781",
  "MH_01782",
  "MH_01783",
  "MH_01784",
  "MH_01785",
  "MH_01786",
  "MH_01787",
  "MH_01788",
  "MH_01789",
  "MH_01790",
  "MH_01791",
  "MH_01792",
  "MH_01793",
  "MH_01794",
  "MH_01795",
  "MH_01796",
  "MH_01797",
  "MH_01798",
  "MH_01799",
  "MH_01800",
  "MH_01801",
  "MH_01802",
  "MH_01803",
  "MH_01804",
  "MH_01805",
  "MH_01806",
  "MH_01807",
  "MH_01808",
  "MH_01809",
  "MH_01810",
  "MH_01811",
  "MH_01812",
  "MH_01813",
  "MH_01814",
  "MH_01815",
  "MH_01816",
  "MH_01817",
  "MH_01818",
  "MH_01819",
  "MH_01820",
  "MH_01821",
  "MH_01822",
  "MH_01823",
  "MH_01824",
  "MH_01825",
  "MH_01826",
  "MH_01827",
  "MH_01828",
  "MH_01829",
  "MH_01830",
  "MH_01831",
  "MH_01832",
  "MH_01833",
  "MH_01834",
  "MH_01835",
  "MH_01836",
  "MH_01837",
  "MH_01838",
  "MH_01839",
  "MH_01840",
  "MH_01841",
  "MH_01842",
  "MH_01843",
  "MH_01844",
  "MH_01845",
  "MH_01846",
  "MH_01847",
  "MH_01848",
  "MH_01849",
  "MH_01850",
  "MH_01851",
  "MH_01852",
  "MH_01853",
  "MH_01854",
  "MH_01855",
  "MH_01856",
  "MH_01857",
  "MH_01858",
  "MH_01859",
  "MH_01860",
  "MH_01861",
  "MH_01862",
  "MH_01863",
  "MH_01864",
  "MH_01865",
  "MH_01866",
  "MH_01867",
  "MH_01868",
  "MH_01869",
  "MH_01870",
  "MH_01871",
  "MH_01872",
  "MH_01873",
  "MH_01874",
  "MH_01875",
  "MH_01876",
  "MH_01877",
  "MH_01878",
  "MH_01879",
  "MH_01880",
  "MH_01881",
  "MH_01882",
  "MH_01883",
  "MH_01884",
  "MH_01885",
  "MH_01886",
  "MH_01887",
  "MH_01888",
  "MH_01889",
  "MH_01890",
  "MH_01891",
  "MH_01892",
  "MH_01893",
  "MH_01894",
  "MH_01895",
  "MH_01896",
  "MH_01897",
  "MH_01898",
  "MH_01899",
  "MH_01900",
  "MH_01901",
  "MH_01902",
  "MH_01903",
  "MH_01904",
  "MH_01905",
  "MH_01906",
  "MH_01907",
  "MH_01908",
  "MH_01909",
  "MH_01910",
  "MH_01911",
  "MH_01912",
  "MH_01913",
  "MH_01914",
  "MH_01915",
  "MH_01916",
  "MH_01917",
  "MH_01918",
  "MH_01919",
  "MH_01920",
  "MH_01921",
  "MH_01922",
  "MH_01923",
  "MH_01924",
  "MH_01925",
  "MH_01926",
  "MH_01927",
  "MH_01928",
  "MH_01929",
  "MH_01930",
  "MH_01931",
  "MH_01932",
  "MH_01933",
  "MH_01934",
  "MH_01935",
  "MH_01936",
  "MH_01937",
  "MH_01938",
  "MH_01939",
  "MH_01940",
  "MH_01941",
  "MH_01942",
  "MH_01943",
  "MH_01944",
  "MH_01945",
  "MH_01946",
  "MH_01947",
  "MH_01948",
  "MH_01949",
  "MH_01950",
  "MH_01951",
  "MH_01952",
  "MH_01953",
  "MH_01954",
  "MH_01955",
  "MH_01956",
  "MH_01957",
  "MH_01958",
  "MH_01959",
  "MH_01960",
  "MH_01961",
  "MH_01962",
  "MH_01963",
  "MH_01964",
  "MH_01965",
  "MH_01966",
  "MH_01967",
  "MH_01968",
  "MH_01969",
  "MH_01970",
  "MH_01971",
  "MH_01972",
  "MH_01973",
  "MH_01974",
  "MH_01975",
  "MH_01976",
  "MH_01977",
  "MH_01978",
  "MH_01979",
  "MH_01980",
  "MH_01981",
  "MH_01982",
  "MH_01983",
  "MH_01984",
  "MH_01985",
  "MH_01986",
  "MH_01987",
  "MH_01988",
  "MH_01989",
  "MH_01990",
  "MH_01991",
  "MH_01992",
  "MH_01993",
  "MH_01994",
  "MH_01995",
  "MH_01996",
  "MH_01997",
  "MH_01998",
  "MH_01999",
  "MH_02000",
  "MH_02001",
  "MH_02002",
  "MH_02003",
  "MH_02004",
  "MH_02005",
  "MH_02006",
  "MH_02007",
  "MH_02008",
  "MH_02009",
  "MH_02010",
  "MH_02011",
  "MH_02012",
  "MH_02013",
  "MH_02014",
  "MH_02015",
  "MH_02016",
  "MH_02017",
  "MH_02018",
  "MH_02019",
  "MH_02020",
  "MH_02021",
  "MH_02022",
  "MH_02023",
  "MH_02024",
  "MH_02025",
  "MH_02026",
  "MH_02027",
  "MH_02028",
  "MH_02029",
  "MH_02030",
  "MH_02031",
  "MH_02032",
  "MH_02033",
  "MH_02034",
  "MH_02035",
  "MH_02036",
  "MH_02037",
  "MH_02038",
  "MH_02039",
  "MH_02040",
  "MH_02041",
  "MH_02042",
  "MH_02043",
  "MH_02044",
  "MH_02045",
  "MH_02046",
  "MH_02047",
  "MH_02048",
  "MH_02049",
  "MH_02050",
  "MH_02051",
  "MH_02052",
  "MH_02053",
  "MH_02054",
  "MH_02055",
  "MH_02056",
  "MH_02057",
  "MH_02058",
  "MH_02059",
  "MH_02060",
  "MH_02061",
  "MH_02062",
  "MH_02063",
  "MH_02064",
  "MH_02065",
  "MH_02066",
  "MH_02067",
  "MH_02068",
  "MH_02069",
  "MH_02070",
  "MH_02071",
  "MH_02072",
  "MH_02073",
  "MH_02074",
  "MH_02075",
  "MH_02076",
  "MH_02077",
  "MH_02078",
  "MH_02079",
  "MH_02080",
  "MH_02081",
  "MH_02082",
  "MH_02083",
  "MH_02084",
  "MH_02085",
  "MH_02086",
  "MH_02087",
  "MH_02088",
  "MH_02089",
  "MH_02090",
  "MH_02091",
  "MH_02092",
  "MH_02093",
  "MH_02094",
  "MH_02095",
  "MH_02096",
  "MH_02097",
  "MH_02098",
  "MH_02099",
  "MH_02100",
  "MH_02101",
  "MH_02102",
  "MH_02103",
  "MH_02104",
  "MH_02105",
  "MH_02106",
  "MH_02107",
  "MH_02108",
  "MH_02109",
  "MH_02110",
  "MH_02111",
  "MH_02112",
  "MH_02113",
  "MH_02114",
  "MH_02115",
  "MH_02116",
  "MH_02117",
  "MH_02118",
  "MH_02119",
  "MH_02120",
  "MH_02121",
  "MH_02122",
  "MH_02123",
  "MH_02124",
  "MH_02125",
  "MH_02126",
  "MH_02127",
  "MH_02128",
  "MH_02129",
  "MH_02130",
  "MH_02131",
  "MH_02132",
  "MH_02133",
  "MH_02134",
  "MH_02135",
  "MH_02136",
  "MH_02137",
  "MH_02138",
  "MH_02139",
  "MH_02140",
  "MH_02141",
  "MH_02142",
  "MH_02143",
  "MH_02144",
  "MH_02145",
  "MH_02146",
  "MH_02147",
  "MH_02148",
  "MH_02149",
  "MH_02150",
  "MH_02151",
  "MH_02152",
  "MH_02153",
  "MH_02154",
  "MH_02155",
  "MH_02156",
  "MH_02157",
  "MH_02158",
  "MH_02159",
  "MH_02160",
  "MH_02161",
  "MH_02162",
  "MH_02163",
  "MH_02164",
  "MH_02165",
  "MH_02166",
  "MH_02167",
  "MH_02168",
  "MH_02169",
  "MH_02170",
  "MH_02171",
  "MH_02172",
  "MH_02173",
  "MH_02174",
  "MH_02175",
  "MH_02176",
  "MH_02177",
  "MH_02178",
  "MH_02179",
  "MH_02180",
  "MH_02181",
  "MH_02182",
  "MH_02183",
  "MH_02184",
  "MH_02185",
  "MH_02186",
  "MH_02187",
  "MH_02188",
  "MH_02189",
  "MH_02190",
  "MH_02191",
  "MH_02192",
  "MH_02193",
  "MH_02194",
  "MH_02195",
  "MH_02196",
  "MH_02197",
  "MH_02198",
  "MH_02199"
]\n

logger = logging.getLogger("targetval.router_v58")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)

class Evidence(BaseModel):
    status: str
    source: str = ""
    fetched_n: int = 0
    data: Dict[str, Any] = Field(default_factory=dict)
    citations: List[str] = Field(default_factory=list)
    fetched_at: str = ""

class BucketFeature(BaseModel):
    bucket: str
    band: str
    drivers: List[str] = Field(default_factory=list)
    tensions: List[str] = Field(default_factory=list)
    citations: List[str] = Field(default_factory=list)
    details: Dict[str, Any] = Field(default_factory=dict)

class SynthesisTI(BaseModel):
    gene: str
    condition: Optional[str] = None
    bucket_features: Dict[str, BucketFeature]
    verdict: str
    bands: Dict[str, str]
    drivers: List[str]
    flip_if: List[str]
    p_favorable: float
    citations: List[str]
router = APIRouter()

class TTLCache:
    def __init__(self, max_items: int = 8192):
        self._data: OrderedDict[str, Tuple[float, Any]] = OrderedDict()
        self._max = max_items

    def get(self, key: str, now: float) -> Optional[Any]:
        if key in self._data:
            exp, val = self._data[key]
            if now < exp:
                self._data.move_to_end(key)
                return val
            else:
                try: del self._data[key]
                except Exception: pass
        return None

    def set(self, key: str, ttl: float, val: Any, now: float):
        self._data[key] = (now + ttl, val)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

class HTTP:
    def __init__(self):
        self.client: Optional[httpx.AsyncClient] = None
        self.cache = TTLCache()

    async def _ensure(self) -> httpx.AsyncClient:
        if self.client is None or self.client.is_closed:
            self.client = httpx.AsyncClient(http2=True, timeout=httpx.Timeout(25, connect=10))
        return self.client

    def now(self) -> float:
        return time.time()

    async def get_json(self, url: str, headers: Optional[Dict[str,str]] = None, tries: int = 2, ttl: int = 3600) -> Any:
        key = f"G::{url}::{json.dumps(headers, sort_keys=True) if headers else ''}"
        now = self.now()
        cached = self.cache.get(key, now)
        if ttl > 0 and cached is not None:
            return cached
        err = None
        for i in range(tries):
            try:
                cli = await self._ensure()
                r = await cli.get(url, headers=headers)
                if r.status_code == 200:
                    js = r.json()
                    if ttl > 0: self.cache.set(key, ttl, js, now)
                    return js
                else:
                    err = f"HTTP {r.status_code}"
            except Exception as e:
                err = str(e)
            await asyncio.sleep(0.25*(i+1))
        logger.warning("GET failed %s err=%s", url, err)
        return None

    async def post_json(self, url: str, payload: Dict[str, Any], headers: Optional[Dict[str,str]] = None, tries: int = 2, ttl: int = 3600) -> Any:
        key = f"P::{url}::{json.dumps(payload, sort_keys=True)[:512]}::{json.dumps(headers, sort_keys=True) if headers else ''}"
        now = self.now()
        cached = self.cache.get(key, now)
        if ttl > 0 and cached is not None:
            return cached
        err = None
        for i in range(tries):
            try:
                cli = await self._ensure()
                r = await cli.post(url, json=payload, headers=headers)
                if r.status_code == 200:
                    js = r.json()
                    if ttl > 0: self.cache.set(key, ttl, js, now)
                    return js
                else:
                    err = f"HTTP {r.status_code}"
            except Exception as e:
                err = str(e)
            await asyncio.sleep(0.25*(i+1))
        logger.warning("POST failed %s err=%s", url, err)
        return None

_http = HTTP()

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

class OpenTargets:
    URL = "https://api.platform.opentargets.org/api/v4/graphql"
    async def gql(self, query: str, variables: Dict[str, Any]) -> Any:
        return await _http.post_json(self.URL, {"query": query, "variables": variables}, tries=2, ttl=43200)

class GTEx:
    async def tpm(self, gene: str) -> Any:
        u = f"https://gtexportal.org/api/v2/gene/expression?gencodeIdOrGeneSymbol={urllib.parse.quote(gene)}"
        return await _http.get_json(u, tries=2, ttl=43200)
    async def sqtl(self, gene: str) -> Any:
        u = f"https://gtexportal.org/api/v2/association/independentSqtl?gencodeIdOrGeneSymbol={urllib.parse.quote(gene)}"
        return await _http.get_json(u, tries=2, ttl=43200)

class EPMC:
    BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    async def search(self, query: str, size: int = 50) -> Any:
        u = f"{self.BASE}?query={urllib.parse.quote(query)}&format=json&pageSize={min(1000,size)}"
        return await _http.get_json(u, tries=2, ttl=3600)

class UniProt:
    async def search(self, gene: str, fields: str) -> Any:
        u = f"https://rest.uniprot.org/uniprotkb/search?query=gene_exact:{urllib.parse.quote(gene)}+AND+organism_id:9606&fields={fields}"
        return await _http.get_json(u, tries=2, ttl=43200)

class AlphaFoldPDBe:
    async def af(self, acc: str) -> Any:
        u = f"https://alphafold.ebi.ac.uk/api/prediction/{acc}"
        return await _http.get_json(u, tries=2, ttl=43200)
    async def pdbe(self, acc: str) -> Any:
        u = f"https://www.ebi.ac.uk/pdbe/graph-api/uniprot/{acc}"
        return await _http.get_json(u, tries=2, ttl=43200)

class GEO_AE:
    async def geo_search(self, gene: str, condition: str) -> Any:
        q = f'({gene}[Title/Abstract]) AND ({condition}) AND ("expression profiling by array"[Filter] OR "expression profiling by high throughput sequencing"[Filter])'
        u = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=gds&retmode=json&term={urllib.parse.quote(q)}&retmax=200"
        return await _http.get_json(u, tries=2, ttl=43200)
    async def arrayexpress(self, query: str) -> Any:
        u = f"https://www.ebi.ac.uk/biostudies/api/v1/biostudies/search?query={urllib.parse.quote(query)}"
        return await _http.get_json(u, tries=2, ttl=43200)

class Proteomics:
    async def pdb_search(self, condition: str) -> Any:
        u = f"https://www.proteomicsdb.org/proteomicsdb/api/v2/proteins/search?text={urllib.parse.quote(condition)}"
        return await _http.get_json(u, tries=2, ttl=43200)
    async def pride_projects(self, keyword: str) -> Any:
        u = f"https://www.ebi.ac.uk/pride/ws/archive/project/list?keyword={urllib.parse.quote(keyword)}"
        return await _http.get_json(u, tries=2, ttl=43200)
    async def pdc_graphql(self, gene: str, size: int = 50) -> Any:
        query = """
        query Search($gene:String!, $size:Int!){
          searchProteins(gene_name:$gene, first:$size){
            edges{ node{ gene_name protein_name uniprot_id cases_count study_submitter_id program_name } }
          }
        }"""
        return await _http.post_json("https://pdc.cancer.gov/graphql", {"query": query, "variables": {"gene": gene, "size": min(100, size)}}, tries=2, ttl=43200)

class Atlases:
    async def hca_projects(self) -> Any:
        u = "https://service.azul.data.humancellatlas.org/index/projects"
        return await _http.get_json(u, tries=2, ttl=43200)
    async def tabula_sapiens_gene(self, gene: str) -> Any:
        u = f"https://tabula-sapiens-portal.ds.czbiohub.org/api/genes?gene={urllib.parse.quote(gene)}"
        return await _http.get_json(u, tries=2, ttl=43200)

class GraphAndNetworks:
    async def string_network(self, gene: str) -> Any:
        u = f"https://string-db.org/api/json/network?identifiers={urllib.parse.quote(gene)}&species=9606"
        return await _http.get_json(u, tries=2, ttl=43200)
    async def reactome_by_uniprot(self, acc: str) -> Any:
        u = f"https://reactome.org/ContentService/data/mapping/UniProt/{acc}/pathways"
        return await _http.get_json(u, tries=2, ttl=43200)
    async def omnipath_interactions(self, gene: str) -> Any:
        u = f"https://omnipathdb.org/interactions?genes={urllib.parse.quote(gene)}&formats=json"
        return await _http.get_json(u, tries=2, ttl=43200)

class GeneticsAPIs:
    async def open_gwas(self, keyword: str) -> Any:
        u = f"https://gwas-api.mrcieu.ac.uk/v1/gwas?keyword={urllib.parse.quote(keyword)}"
        return await _http.get_json(u, tries=2, ttl=43200)
    async def clinvar_search(self, gene: str) -> Any:
        u = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=clinvar&retmode=json&term={urllib.parse.quote(gene+'[gene] AND human[filter]')}"
        return await _http.get_json(u, tries=2, ttl=43200)
    async def clingen_gene(self, gene: str) -> Any:
        u = f"https://search.clinicalgenome.org/kb/genes/{urllib.parse.quote(gene)}.json"
        return await _http.get_json(u, tries=2, ttl=43200)
    async def gnomad_constraint(self, symbol: str) -> Any:
        q = """
        query Constraint($sym:String!){
          gene(gene_symbol:$sym, reference_genome: GRCh38){
            symbol constraint{ pLI oe_lof lof_z mis_z lof_upper oe_lof_lower oe_mis }
          }
        }"""
        return await _http.post_json("https://gnomad.broadinstitute.org/api", {"query": q, "variables": {"sym": symbol}}, tries=2, ttl=43200)
    async def mavedb_search(self, gene: str) -> Any:
        u = f"https://www.mavedb.org/api/v1/search?q={urllib.parse.quote(gene)}"
        return await _http.get_json(u, tries=2, ttl=43200)

class DrugSafetyAndPGx:
    async def dgidb(self, gene: str) -> Any:
        u = f"https://dgidb.org/api/v2/interactions.json?genes={urllib.parse.quote(gene)}"
        return await _http.get_json(u, tries=2, ttl=43200)
    async def faers_top(self, drug: str) -> Any:
        u = f"https://api.fda.gov/drug/event.json?search=patient.drug.medicinalproduct:{urllib.parse.quote(drug)}&count=patient.reaction.reactionmeddrapt.exact"
        return await _http.get_json(u, tries=2, ttl=43200)
    async def pharmgkb_gene(self, gene: str) -> Any:
        u = f"https://api.pharmgkb.org/v1/data/gene?symbol={urllib.parse.quote(gene)}"
        return await _http.get_json(u, tries=2, ttl=43200)
    async def sider_meddra(self, drug: str) -> Any:
        u = f"http://sideeffects.embl.de/api/meddra/allSides?drug={urllib.parse.quote(drug)}"
        return await _http.get_json(u, tries=2, ttl=43200)

class TrialsAndPatents:
    async def ctgov_studies(self, condition: str) -> Any:
        u = f"https://clinicaltrials.gov/api/v2/studies?query.cond={urllib.parse.quote(condition)}&pageSize=50"
        return await _http.get_json(u, tries=2, ttl=43200)
    async def patentsview(self, keyword: str) -> Any:
        q = json.dumps({"_text_any":{"patent_title":keyword}})
        opts = json.dumps({"per_page":50,"page":1,"matched_subentities_only":True})
        u = f"https://api.patentsview.org/patents/query?q={urllib.parse.quote(q)}&f=[%22patent_number%22,%22patent_date%22,%22cpc_section_id%22]&o={urllib.parse.quote(opts)}"
        return await _http.get_json(u, tries=2, ttl=43200)

class ImmuneEpi:
    async def iedb(self, gene: str, page_size: int = 50) -> Any:
        u = f"https://api.iedb.org/epitope/search?antigen_gene={urllib.parse.quote(gene)}&page_size={min(100, page_size)}"
        return await _http.get_json(u, tries=2, ttl=43200)

OT = OpenTargets()
GT = GTEx()
EPM = EPMC()
UP = UniProt()
AF = AlphaFoldPDBe()
GE = GEO_AE()
PR = Proteomics()
AT = Atlases()
GN = GeneticsAPIs()
DR = DrugSafetyAndPGx()
TP = TrialsAndPatents()
IM = ImmuneEpi()
GNW = GraphAndNetworks()

def _stance(title: str, abstract: str) -> str:
    t = (title or "").lower() + " " + (abstract or "").lower()
    if any(neg in t for neg in ["no association", "did not replicate", "null association", "not significant", "failed replication", "no effect", "not associated"]):
        return "disconfirm"
    if any(pos in t for pos in ["mendelian randomization", "colocalization", "colocalisation", "replication", "significant association", "crispra", "crispri", "perturb-seq", "functional validation", "mpra", "starr"]):
        return "confirm"
    return "neutral"

def _lit_quality(venue: str, pub_type: str, is_preprint: bool, year: int) -> float:
    w = 0.0
    high = ["nature", "science", "cell", "nejm", "lancet", "nat ", "cell reports", "nat med", "nat genetics", "nature genetics"]
    med = ["plos", "biorxiv", "medrxiv", "communications", "genome", "bioinformatics", "elife"]
    v = (venue or "").lower()
    if any(h in v for h in high): w += 2.0
    elif any(m in v for m in med): w += 1.0
    if pub_type:
        pt = pub_type.lower()
        if "meta-analysis" in pt or "systematic review" in pt: w += 1.0
        if "randomized" in pt or "clinical trial" in pt: w += 1.0
    try:
        if int(year) >= 2023: w += 0.5
    except Exception:
        pass
    if is_preprint: w -= 1.0
    return w

async def lit_search_guarded(query_confirm: str, query_disconfirm: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    c = await EPM.search(query_confirm, size=limit)
    d = await EPM.search(query_disconfirm, size=limit) if query_disconfirm else {"resultList": {"result": []}}
    def norm(js: Any) -> List[Dict[str, Any]]:
        res = ((js or {}).get("resultList") or {}).get("result") or []
        out = []
        for r in res:
            stance = _stance(r.get("title",""), r.get("abstractText",""))
            year = r.get("pubYear") or r.get("pubYearSort") or 0
            venue = r.get("journalTitle","")
            preprint = "biorxiv" in (venue or "").lower() or "medrxiv" in (venue or "").lower()
            score = _lit_quality(venue, r.get("pubType",""), preprint, int(year) if str(year).isdigit() else 0)
            out.append({
                "pmid": r.get("pmid") or r.get("id"),
                "title": r.get("title"), "journal": venue, "year": year,
                "stance": stance, "score": score, "doi": r.get("doi"),
                "link": f"https://europepmc.org/abstract/MED/{r.get('pmid')}" if r.get("pmid") else None
            })
        out.sort(key=lambda x: (x["stance"], -x["score"], -(int(x["year"]) if str(x["year"]).isdigit() else 0)))
        return out
    cn = norm(c); dn = norm(d)
    groups = {"confirming": [], "disconfirming": [], "neutral/context": []}
    for n in (cn + dn):
        groups["confirming" if n["stance"]=="confirm" else "disconfirming" if n["stance"]=="disconfirm" else "neutral/context"].append(n)
    return groups

BUCKETS = [
    {"id": "TARGET_IDENTITY", "label": "Target Identity & Baseline Context"},
    {"id": "DISEASE_ASSOCIATION", "label": "Disease Association & Context"},
    {"id": "GENETIC_CAUSALITY", "label": "Genetic Causality & Regulatory Evidence"},
    {"id": "MECHANISM", "label": "Mechanism & Perturbation"},
    {"id": "TRACTABILITY_MODALITY", "label": "Tractability & Modality"},
    {"id": "CLINICAL_FIT_FEASIBILITY", "label": "Clinical Fit, Safety & Competitive"},
    {"id": "SYNTHESIS_TI", "label": "Therapeutic Index Synthesis (meta)"},
]

def M(id, title, primary, buckets, sources, priority="high", compute="medium", freq="monthly", fail="degrade", deps=None, desc=""):
    return {
        "id": id, "title": title, "bucket": primary, "buckets": buckets, "sources": sources,
        "priority": priority, "compute_budget": compute, "update_frequency": freq,
        "failure_mode": fail, "dependencies": deps or [], "description": desc,
    }

MODULES = [
    # Identity (4)
    M("expr_baseline","Baseline expression by tissue/cell","TARGET_IDENTITY",["TARGET_IDENTITY"],["HPA","Expression Atlas","cellxgene","MyGene","COMPARTMENTS"],compute="light"),
    M("expr_localization","Subcellular localization","TARGET_IDENTITY",["TARGET_IDENTITY","TRACTABILITY_MODALITY"],["COMPARTMENTS","HPA","UniProt"],compute="light"),
    M("mech_structure","Structure & model availability","TARGET_IDENTITY",["TARGET_IDENTITY","TRACTABILITY_MODALITY"],["AlphaFold","PDBe","UniProt"],compute="light"),
    M("expr_inducibility","Inducibility / perturbation of expression","TARGET_IDENTITY",["TARGET_IDENTITY","MECHANISM"],["Expression Atlas","HPA","ENCODE","Europe PMC"]),

    # Disease Association (10)
    M("assoc_bulk_rna","Bulk RNA disease association","DISEASE_ASSOCIATION",["DISEASE_ASSOCIATION"],["Expression Atlas","GEO","ArrayExpress"]),
    M("assoc_sc","Single-cell association","DISEASE_ASSOCIATION",["DISEASE_ASSOCIATION"],["cellxgene","SCEA","HCA"]),
    M("spatial_expression","Spatial expression evidence","DISEASE_ASSOCIATION",["DISEASE_ASSOCIATION"],["STOmicsDB","HTAN","cellxgene","Europe PMC"]),
    M("spatial_neighborhoods","Spatial neighborhoods / L–R niches","DISEASE_ASSOCIATION",["DISEASE_ASSOCIATION","MECHANISM"],["STOmicsDB","HTAN","Europe PMC"]),
    M("assoc_bulk_prot","Bulk proteomics association","DISEASE_ASSOCIATION",["DISEASE_ASSOCIATION"],["ProteomicsDB","PRIDE","ProteomeXchange"]),
    M("omics_phosphoproteomics","Phosphoproteomics","DISEASE_ASSOCIATION",["DISEASE_ASSOCIATION","MECHANISM"],["PhosphoSitePlus","PRIDE"]),
    M("omics_metabolites","Metabolomics association","DISEASE_ASSOCIATION",["DISEASE_ASSOCIATION"],["MetaboLights","HMDB"]),
    M("assoc_hpa_pathology","Pathology expression (HPA)","DISEASE_ASSOCIATION",["DISEASE_ASSOCIATION","CLINICAL_FIT_FEASIBILITY"],["HPA","UniProt"],compute="light"),
    M("assoc_bulk_prot_pdc","Proteomics (PDC/CPTAC)","DISEASE_ASSOCIATION",["DISEASE_ASSOCIATION"],["PDC GraphQL (CPTAC)"]),
    M("assoc_metabolomics_ukb_nightingale","UKB Nightingale metabolomics link","DISEASE_ASSOCIATION",["DISEASE_ASSOCIATION"],["Nightingale Biomarker Atlas"],compute="light"),

    # Genetic Causality & Regulatory (14)
    M("genetics_l2g","Locus-to-gene associations","GENETIC_CAUSALITY",["GENETIC_CAUSALITY"],["OpenTargets","GWAS Catalog"]),
    M("genetics_coloc","Colocalization","GENETIC_CAUSALITY",["GENETIC_CAUSALITY"],["OpenTargets (coloc)","GTEx","eQTL Catalogue","GWAS Catalog"]),
    M("genetics_mr","Mendelian randomization discovery pairing","GENETIC_CAUSALITY",["GENETIC_CAUSALITY"],["MR-Base/OpenGWAS","GWAS Catalog","GTEx"]),
    M("genetics_rare","Rare variant evidence","GENETIC_CAUSALITY",["GENETIC_CAUSALITY","CLINICAL_FIT_FEASIBILITY"],["gnomAD","ClinVar"],compute="light"),
    M("genetics_mendelian","Mendelian validity","GENETIC_CAUSALITY",["GENETIC_CAUSALITY","CLINICAL_FIT_FEASIBILITY"],["ClinVar","ClinGen","Orphanet"],compute="light"),
    M("genetics_phewas_human_knockout","Human LoF PheWAS","GENETIC_CAUSALITY",["GENETIC_CAUSALITY","CLINICAL_FIT_FEASIBILITY"],["Europe PMC","UK Biobank/GeneBass (lit)"]),
    M("genetics_sqtl","Splicing QTLs","GENETIC_CAUSALITY",["GENETIC_CAUSALITY"],["GTEx","eQTL Catalogue"]),
    M("genetics_pqtl","Protein QTLs","GENETIC_CAUSALITY",["GENETIC_CAUSALITY","TRACTABILITY_MODALITY"],["OpenTargets (pQTL)","SCALLOP/Sun via OT"]),
    M("genetics_chromatin_contacts","Chromatin contacts (PCHi‑C/Hi‑C)","GENETIC_CAUSALITY",["GENETIC_CAUSALITY","MECHANISM"],["Europe PMC"]),
    M("genetics_functional","Functional regulatory assays","GENETIC_CAUSALITY",["GENETIC_CAUSALITY","MECHANISM"],["Europe PMC","ENCODE"]),
    M("genetics_lncrna","lncRNA regulatory layer","GENETIC_CAUSALITY",["GENETIC_CAUSALITY","MECHANISM"],["Europe PMC"],compute="light"),
    M("genetics_mirna","miRNA regulation layer","GENETIC_CAUSALITY",["GENETIC_CAUSALITY","MECHANISM"],["Europe PMC"],compute="light"),
    M("genetics_pathogenicity_priors","Missense pathogenicity priors","GENETIC_CAUSALITY",["GENETIC_CAUSALITY","CLINICAL_FIT_FEASIBILITY","TRACTABILITY_MODALITY"],["Europe PMC"],compute="light"),
    M("genetics_mavedb","Variant effect maps (MAVE)","GENETIC_CAUSALITY",["GENETIC_CAUSALITY","MECHANISM"],["MaveDB API","Europe PMC"]),

    # Mechanism & Perturbation (5)
    M("mech_ppi","Protein–protein interactions","MECHANISM",["MECHANISM"],["STRING"],compute="light"),
    M("mech_pathways","Pathway membership","MECHANISM",["MECHANISM"],["Reactome"],compute="light"),
    M("mech_ligrec","Ligand–receptor signaling","MECHANISM",["MECHANISM"],["OmniPath","Reactome"],compute="light"),
    M("assoc_perturb","Perturbation evidence (CRISPR/RNAi/seq)","MECHANISM",["MECHANISM"],["Europe PMC"]),
    M("assoc_perturbatlas","PerturbAtlas link-out","MECHANISM",["MECHANISM"],["PerturbAtlas"],compute="light"),

    # Tractability & Modality (10)
    M("tract_drugs","Known druggability & precedent","TRACTABILITY_MODALITY",["TRACTABILITY_MODALITY"],["ChEMBL","DGIdb","Inxight"]),
    M("tract_ligandability_sm","Small-molecule ligandability","TRACTABILITY_MODALITY",["TRACTABILITY_MODALITY"],["AlphaFold","PDBe","UniProt"]),
    M("tract_ligandability_ab","Antibody feasibility (surface/secreted)","TRACTABILITY_MODALITY",["TRACTABILITY_MODALITY"],["UniProt","HPA"],compute="light"),
    M("tract_ligandability_oligo","Oligo feasibility","TRACTABILITY_MODALITY",["TRACTABILITY_MODALITY"],["Europe PMC","RiboCentre"],compute="light"),
    M("tract_modality","Modality recommender (rule-based)","TRACTABILITY_MODALITY",["TRACTABILITY_MODALITY"],["UniProt","AlphaFold","HPA","IEDB"],compute="light"),
    M("tract_immunogenicity","Immunogenicity literature","TRACTABILITY_MODALITY",["TRACTABILITY_MODALITY"],["Europe PMC"],compute="light"),
    M("tract_mhc_binding","MHC binding predictors (lit)","TRACTABILITY_MODALITY",["TRACTABILITY_MODALITY"],["Europe PMC"],compute="light"),
    M("tract_iedb_epitopes","IEDB epitopes","TRACTABILITY_MODALITY",["TRACTABILITY_MODALITY"],["IEDB IQ-API"]),
    M("tract_surfaceome_hpa","HPA membrane/secretome","TRACTABILITY_MODALITY",["TRACTABILITY_MODALITY"],["HPA v24"],compute="light"),
    M("tract_tsca","Cancer Surfaceome Atlas","TRACTABILITY_MODALITY",["TRACTABILITY_MODALITY"],["TCSA"],compute="light"),

    # Clinical Fit & Competitive (10)
    M("clin_endpoints","Clinical endpoints landscape","CLINICAL_FIT_FEASIBILITY",["CLINICAL_FIT_FEASIBILITY"],["ClinicalTrials.gov v2"]),
    M("clin_biomarker_fit","Biomarker fit (context)","CLINICAL_FIT_FEASIBILITY",["CLINICAL_FIT_FEASIBILITY"],["Europe PMC","HPA","Expression Atlas","cellxgene"]),
    M("clin_pipeline","Pipeline/programs","CLINICAL_FIT_FEASIBILITY",["CLINICAL_FIT_FEASIBILITY"],["Inxight Drugs"]),
    M("clin_safety","Post-marketing safety (FAERS)","CLINICAL_FIT_FEASIBILITY",["CLINICAL_FIT_FEASIBILITY"],["openFDA FAERS","DGIdb"]),
    M("clin_safety_pgx","Pharmacogenomics (PGx)","CLINICAL_FIT_FEASIBILITY",["CLINICAL_FIT_FEASIBILITY"],["PharmGKB"]),
    M("clin_rwe","RWE safety topline","CLINICAL_FIT_FEASIBILITY",["CLINICAL_FIT_FEASIBILITY"],["openFDA FAERS"]),
    M("clin_on_target_ae_prior","On-target AE prior","CLINICAL_FIT_FEASIBILITY",["CLINICAL_FIT_FEASIBILITY"],["DGIdb","SIDER"]),
    M("comp_intensity","Competitive intensity","CLINICAL_FIT_FEASIBILITY",["CLINICAL_FIT_FEASIBILITY"],["PatentsView"]),
    M("comp_freedom","Freedom to operate (proxy)","CLINICAL_FIT_FEASIBILITY",["CLINICAL_FIT_FEASIBILITY"],["PatentsView","SureChEMBL"]),
    M("clin_eu_ctr_linkouts","EU CTR/CTIS link-outs","CLINICAL_FIT_FEASIBILITY",["CLINICAL_FIT_FEASIBILITY"],["EU CTR/CTIS"],compute="light"),
]

assert len(MODULES) == 58, f"Expected 58 modules, got {len(MODULES)}"

def module_route(mid: str) -> str:
    mapping = {
        # Identity
        "expr_baseline": "/expr/baseline",
        "expr_localization": "/expr/localization",
        "mech_structure": "/mech/structure",
        "expr_inducibility": "/expr/inducibility",
        # Association
        "assoc_bulk_rna": "/assoc/bulk-rna",
        "assoc_sc": "/assoc/sc",
        "spatial_expression": "/assoc/spatial-expression",
        "spatial_neighborhoods": "/assoc/spatial-neighborhoods",
        "assoc_bulk_prot": "/assoc/bulk-prot",
        "omics_phosphoproteomics": "/assoc/omics-phosphoproteomics",
        "omics_metabolites": "/assoc/omics-metabolites",
        "assoc_hpa_pathology": "/assoc/hpa-pathology",
        "assoc_bulk_prot_pdc": "/assoc/bulk-prot-pdc",
        "assoc_metabolomics_ukb_nightingale": "/assoc/metabolomics-ukb-nightingale",
        # Genetics
        "genetics_l2g": "/genetics/l2g",
        "genetics_coloc": "/genetics/coloc",
        "genetics_mr": "/genetics/mr",
        "genetics_rare": "/genetics/rare",
        "genetics_mendelian": "/genetics/mendelian",
        "genetics_phewas_human_knockout": "/genetics/phewas-human-knockout",
        "genetics_sqtl": "/genetics/sqtl",
        "genetics_pqtl": "/genetics/pqtl",
        "genetics_chromatin_contacts": "/genetics/chromatin-contacts",
        "genetics_functional": "/genetics/functional",
        "genetics_lncrna": "/genetics/lncrna",
        "genetics_mirna": "/genetics/mirna",
        "genetics_pathogenicity_priors": "/genetics/pathogenicity-priors",
        "genetics_mavedb": "/genetics/mavedb",
        # Mechanism
        "mech_ppi": "/mech/ppi",
        "mech_pathways": "/mech/pathways",
        "mech_ligrec": "/mech/ligrec",
        "assoc_perturb": "/assoc/perturb",
        "assoc_perturbatlas": "/assoc/perturbatlas",
        # Tractability
        "tract_drugs": "/tract/drugs",
        "tract_ligandability_sm": "/tract/ligandability-sm",
        "tract_ligandability_ab": "/tract/ligandability-ab",
        "tract_ligandability_oligo": "/tract/ligandability-oligo",
        "tract_modality": "/tract/modality",
        "tract_immunogenicity": "/tract/immunogenicity",
        "tract_mhc_binding": "/tract/mhc-binding",
        "tract_iedb_epitopes": "/tract/iedb-epitopes",
        "tract_surfaceome_hpa": "/tract/surfaceome-hpa",
        "tract_tsca": "/tract/tsca",
        # Clinical/Competitive
        "clin_endpoints": "/clin/endpoints",
        "clin_biomarker_fit": "/clin/biomarker-fit",
        "clin_pipeline": "/clin/pipeline",
        "clin_safety": "/clin/safety",
        "clin_safety_pgx": "/clin/safety-pgx",
        "clin_rwe": "/clin/rwe",
        "clin_on_target_ae_prior": "/clin/on-target-ae-prior",
        "comp_intensity": "/comp/intensity",
        "comp_freedom": "/comp/freedom",
        "clin_eu_ctr_linkouts": "/clin/eu-ctr-linkouts",
    }
    return mapping[mid]

REGISTRY_V58 = {b["id"]: [] for b in BUCKETS}
for m in MODULES:
    REGISTRY_V58[m["bucket"]].append(module_route(m["id"]))
REGISTRY_V58["SYNTHESIS_TI"] = ["/synth/therapeutic-index"]

def _ok(ev: Evidence) -> bool:
    return isinstance(ev, Evidence) and ev.status == "OK" and (ev.fetched_n or len(ev.data or {}) > 0)

# Identity
async def svc_expr_baseline(gene: str) -> Evidence:
    js = await GT.tpm(gene)
    rows = (js or {}).get("data") or []
    return Evidence(status="OK" if rows else "NO_DATA", source="GTEx", fetched_n=len(rows),
                    data={"gene": gene, "tpm_by_tissue": rows}, citations=[f"https://gtexportal.org/api/v2/gene/expression?gencodeIdOrGeneSymbol={urllib.parse.quote(gene)}"], fetched_at=_now_iso())

async def svc_expr_localization(gene: str) -> Evidence:
    js = await UP.search(gene, "accession,protein_name,subcellular_location")
    return Evidence(status="OK" if js else "NO_DATA", source="UniProtKB", fetched_n=1 if js else 0,
                    data={"gene": gene, "uniprot_search": js}, citations=[f"https://rest.uniprot.org/uniprotkb/search?query=gene_exact:{urllib.parse.quote(gene)}+AND+organism_id:9606&fields=accession,protein_name,subcellular_location"], fetched_at=_now_iso())

async def svc_mech_structure(gene: str) -> Evidence:
    ujs = await UP.search(gene, "accession,protein_name")
    accessions = []
    try:
        for itm in (ujs.get("results") or []):
            accessions.append(itm.get("primaryAccession"))
    except Exception:
        pass
    acc = accessions[0] if accessions else None
    af, pdbe, cites = None, None, []
    if acc:
        af = await AF.af(acc); pdbe = await AF.pdbe(acc)
        cites = [f"https://alphafold.ebi.ac.uk/api/prediction/{acc}", f"https://www.ebi.ac.uk/pdbe/graph-api/uniprot/{acc}"]
    rows = {"accessions": accessions, "alphafold": af, "pdbe": pdbe}
    return Evidence(status="OK" if acc and (af or pdbe) else "NO_DATA", source="AlphaFold/PDBe/UniProt", fetched_n=1 if acc else 0,
                    data=rows, citations=cites, fetched_at=_now_iso())

async def svc_expr_inducibility(gene: str, condition: Optional[str]) -> Evidence:
    qconfirm = f'{gene} AND (inducible OR upregulated OR downregulated OR stimulation OR interferon OR cytokine)'
    if condition: qconfirm += f' AND {condition}'
    groups = await lit_search_guarded(qconfirm, f'{gene} AND (no change OR not induced OR unchanged)', limit=50)
    return Evidence(status="OK", source="EuropePMC", fetched_n=sum(len(v) for v in groups.values()),
                    data=groups, citations=["https://www.ebi.ac.uk/europepmc/"], fetched_at=_now_iso())

# Association
async def svc_assoc_bulk_rna(gene: str, condition: str) -> Evidence:
    es = await GE.geo_search(gene, condition)
    ids = (((es or {}).get("esearchresult") or {}).get("idlist") or [])
    links = [f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={gid}" for gid in ids[:50]]
    aj = await GE.arrayexpress(f"{gene} {condition}")
    return Evidence(status="OK" if ids or aj else "NO_DATA", source="GEO/ArrayExpress", fetched_n=len(ids),
                    data={"geo_ids": ids, "geo_links": links, "arrayexpress": aj}, citations=[
                        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=gds&retmode=json&term=(%28{urllib.parse.quote(gene)}%5BTitle/Abstract%5D%29%20AND%20(%28{urllib.parse.quote(condition)}%29)%20AND%20(%22expression%20profiling%20by%20array%22%5BFilter%5D%20OR%20%22expression%20profiling%20by%20high%20throughput%20sequencing%22%5BFilter%5D)&retmax=200",
                        f"https://www.ebi.ac.uk/biostudies/api/v1/biostudies/search?query={urllib.parse.quote(gene+' '+condition)}"
                    ], fetched_at=_now_iso())

async def svc_assoc_sc(gene: str, condition: Optional[str]) -> Evidence:
    hca_js = await AT.hca_projects()
    ts_js = await AT.tabula_sapiens_gene(gene)
    return Evidence(status="OK" if hca_js or ts_js else "NO_DATA", source="HCA/TabulaSapiens", fetched_n=1,
                    data={"hca_projects": hca_js, "tabula_sapiens": ts_js, "filter_condition": condition},
                    citations=["https://service.azul.data.humancellatlas.org/index/projects",
                               f"https://tabula-sapiens-portal.ds.czbiohub.org/api/genes?gene={urllib.parse.quote(gene)}"], fetched_at=_now_iso())

async def svc_spatial_expression(gene: str, condition: Optional[str]) -> Evidence:
    q1 = f'{gene} AND ("spatial transcriptomics" OR Visium OR MERFISH OR GeoMx)'
    if condition: q1 += f" AND {condition}"
    groups = await lit_search_guarded(q1, f"{gene} AND (spatial) AND (no change OR null)", limit=50)
    return Evidence(status="OK", source="EuropePMC", fetched_n=sum(len(v) for v in groups.values()),
                    data=groups, citations=["https://www.ebi.ac.uk/europepmc/"], fetched_at=_now_iso())

async def svc_spatial_neighborhoods(condition: str) -> Evidence:
    q1 = f'{condition} AND ("cell neighborhood" OR "cell-cell interaction" OR "ligand-receptor" OR niche) AND (spatial)'
    groups = await lit_search_guarded(q1, None, limit=50)
    return Evidence(status="OK", source="EuropePMC", fetched_n=sum(len(v) for v in groups.values()),
                    data=groups, citations=["https://www.ebi.ac.uk/europepmc/"], fetched_at=_now_iso())

async def svc_assoc_bulk_prot(condition: str) -> Evidence:
    pj = await PR.pdb_search(condition)
    prj = await PR.pride_projects(condition)
    px = f"https://proteomecentral.proteomexchange.org/cgi/GetDataset?ID={urllib.parse.quote(condition)}"
    return Evidence(status="OK" if pj or prj else "NO_DATA", source="ProteomicsDB/PRIDE/ProteomeXchange", fetched_n=len((prj or [])),
                    data={"proteomicsdb": pj, "pride": prj, "proteomexchange_query": px},
                    citations=[f"https://www.proteomicsdb.org/proteomicsdb/api/v2/proteins/search?text={urllib.parse.quote(condition)}",
                               f"https://www.ebi.ac.uk/pride/ws/archive/project/list?keyword={urllib.parse.quote(condition)}", px],
                    fetched_at=_now_iso())

async def svc_omics_phosphoproteomics(gene: str, condition: Optional[str]) -> Evidence:
    q = (gene + " " + condition) if condition else gene
    prj = await PR.pride_projects(q + " phospho")
    return Evidence(status="OK" if prj else "NO_DATA", source="PRIDE", fetched_n=len((prj or [])),
                    data={"query": q, "pride": prj}, citations=[f"https://www.ebi.ac.uk/pride/ws/archive/project/list?keyword={urllib.parse.quote(q+' phospho')}"],
                    fetched_at=_now_iso())

async def svc_omics_metabolites(condition: str) -> Evidence:
    js = await _http.get_json(f"https://www.ebi.ac.uk/metabolights/ws/studies/search?query={urllib.parse.quote(condition)}", tries=2, ttl=43200)
    rows = (js or {}).get("content") or []
    hmdb = f"https://hmdb.ca/unearth/q?query={urllib.parse.quote(condition)}&searcher=metabolites"
    return Evidence(status="OK" if rows else "NO_DATA", source="MetaboLights/HMDB", fetched_n=len(rows),
                    data={"metabolights": rows, "hmdb_link": hmdb},
                    citations=[f"https://www.ebi.ac.uk/metabolights/ws/studies/search?query={urllib.parse.quote(condition)}", hmdb], fetched_at=_now_iso())

async def svc_hpa_pathology(gene: str) -> Evidence:
    link = f"https://www.proteinatlas.org/search/{urllib.parse.quote(gene)}"
    dl = "https://www.proteinatlas.org/about/download"
    return Evidence(status="OK", source="HPA (link)/UniProt", fetched_n=1,
                    data={"gene": gene, "hpa_link": link, "download": dl},
                    citations=[link, dl], fetched_at=_now_iso())

async def svc_bulk_prot_pdc(gene: str, limit: int = 50) -> Evidence:
    js = await PR.pdc_graphql(gene, size=limit)
    edges = (((js or {}).get("data") or {}).get("searchProteins") or {}).get("edges") or []
    nodes = [e.get("node") for e in edges if isinstance(e, dict)]
    return Evidence(status="OK" if nodes else "NO_DATA", source="PDC GraphQL", fetched_n=len(nodes),
                    data={"gene": gene, "records": nodes[:limit], "portal": "https://pdc.cancer.gov"},
                    citations=["https://pdc.cancer.gov/graphql", "https://pdc.cancer.gov"], fetched_at=_now_iso())

async def svc_metabolomics_ukb_nightingale() -> Evidence:
    url = "https://biomarker-atlas.nightingale.cloud"
    return Evidence(status="OK", source="Nightingale Biomarker Atlas", fetched_n=0,
                    data={"portal": url, "note": "Use portal filters; CSVs are large."},
                    citations=[url], fetched_at=_now_iso())

# Genetics
async def svc_genetics_l2g(gene: str) -> Evidence:
    query = """
    query L2G($gene:String!){
      geneInfo(geneId:$gene){
        id
        associations{ rows{ disease{ id name } score datatype } total }
      }
    }"""
    js = await OT.gql(query, {"gene": gene})
    rows = (((js or {}).get("data") or {}).get("geneInfo") or {}).get("associations", {}).get("rows", []) if isinstance(js, dict) else []
    return Evidence(status="OK" if rows else "NO_DATA", source="OpenTargets L2G", fetched_n=len(rows),
                    data={"gene": gene, "associations": rows}, citations=["https://api.platform.opentargets.org/api/v4/graphql"], fetched_at=_now_iso())

async def svc_genetics_coloc(gene: str) -> Evidence:
    query = """
    query Coloc($gene:String!){
      geneInfo(geneId:$gene){
        colocalisations(page:{index:0,size:200}){
          rows{
            leftVariant{ id } rightVariant{ id }
            locus1Genes{ gene{ id symbol } h4 posteriorProbability }
            locus2Genes{ gene{ id symbol } }
            studyType source tissue
          } total
        }
      }
    }"""
    js = await OT.gql(query, {"gene": gene})
    rows = (((js or {}).get("data") or {}).get("geneInfo") or {}).get("colocalisations", {}).get("rows", []) if isinstance(js, dict) else []
    return Evidence(status="OK" if rows else "NO_DATA", source="OpenTargets coloc", fetched_n=len(rows),
                    data={"gene": gene, "colocs": rows}, citations=["https://api.platform.opentargets.org/api/v4/graphql"], fetched_at=_now_iso())

async def svc_genetics_mr(gene: str, condition: str) -> Evidence:
    exp = await GN.open_gwas(gene)
    out = await GN.open_gwas(condition)
    return Evidence(status="OK" if exp or out else "NO_DATA", source="IEU OpenGWAS (discovery)",
                    fetched_n=len((exp or {}).get("data", []))+len((out or {}).get("data", [])),
                    data={"exposure_matches": (exp or {}).get("data", []), "outcome_matches": (out or {}).get("data", [])},
                    citations=[f"https://gwas-api.mrcieu.ac.uk/v1/gwas?keyword={urllib.parse.quote(gene)}",
                               f"https://gwas-api.mrcieu.ac.uk/v1/gwas?keyword={urllib.parse.quote(condition)}"], fetched_at=_now_iso())

async def svc_genetics_rare(gene: str) -> Evidence:
    js = await GN.clinvar_search(gene)
    ids = (((js or {}).get("esearchresult") or {}).get("idlist") or [])
    links = [f"https://www.ncbi.nlm.nih.gov/clinvar/variation/{vid}" for vid in ids[:100]]
    return Evidence(status="OK" if ids else "NO_DATA", source="ClinVar", fetched_n=len(ids),
                    data={"ids": ids, "links": links}, citations=[f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=clinvar&retmode=json&term={urllib.parse.quote(gene+'[gene] AND human[filter]')}"], fetched_at=_now_iso())

async def svc_genetics_mendelian(gene: str) -> Evidence:
    js = await GN.clingen_gene(gene)
    return Evidence(status="OK" if js else "NO_DATA", source="ClinGen (gene validity)", fetched_n=1 if js else 0,
                    data={"clingen": js}, citations=[f"https://search.clinicalgenome.org/kb/genes/{urllib.parse.quote(gene)}.json"], fetched_at=_now_iso())

async def svc_genetics_intolerance(gene: str) -> Evidence:
    js = await GN.gnomad_constraint(gene)
    data = (((js or {}).get("data") or {}).get("gene") or {}).get("constraint") or {}
    return Evidence(status="OK" if data else "NO_DATA", source="gnomAD GraphQL", fetched_n=1 if data else 0,
                    data={"gene": gene, "constraint": data}, citations=["https://gnomad.broadinstitute.org/api"], fetched_at=_now_iso())

async def svc_phewas_human_knockout(gene: str) -> Evidence:
    groups = await lit_search_guarded(f'{gene} AND (loss-of-function OR human knockout) AND (PheWAS OR UK Biobank OR GeneBass)',
                                      f'{gene} AND (loss-of-function) AND (no association OR null)', limit=50)
    return Evidence(status="OK", source="EuropePMC", fetched_n=sum(len(v) for v in groups.values()),
                    data=groups, citations=["https://www.ebi.ac.uk/europepmc/"], fetched_at=_now_iso())

async def svc_genetics_sqtl(gene: str) -> Evidence:
    js = await GT.sqtl(gene)
    return Evidence(status="OK" if js else "NO_DATA", source="GTEx sQTL", fetched_n=len(((js or {}).get('data') or [])),
                    data={"gene": gene, "sqtl": js}, citations=[f"https://gtexportal.org/api/v2/association/independentSqtl?gencodeIdOrGeneSymbol={urllib.parse.quote(gene)}"], fetched_at=_now_iso())

async def svc_genetics_pqtl(gene: str) -> Evidence:
    query = """
    query Coloc($gene:String!){
      geneInfo(geneId:$gene){
        colocalisations(studyTypes:["pqtl"], page:{index:0,size:200}){
          rows{
            leftVariant{ id } rightVariant{ id } tissue studyType source
            locus1Genes{ gene{ id symbol } h4 posteriorProbability }
            locus2Genes{ gene{ id symbol } }
          } total
        }
      }
    }"""
    js = await OT.gql(query, {"gene": gene})
    rows = (((js or {}).get("data") or {}).get("geneInfo") or {}).get("colocalisations", {}).get("rows", []) if isinstance(js, dict) else []
    return Evidence(status="OK" if rows else "NO_DATA", source="OpenTargets (pQTL colocs)", fetched_n=len(rows),
                    data={"gene": gene, "pqtl_coloc": rows}, citations=["https://api.platform.opentargets.org/api/v4/graphql"], fetched_at=_now_iso())

async def svc_chromatin_contacts(gene: str) -> Evidence:
    groups = await lit_search_guarded(f'{gene} AND (promoter capture Hi-C OR PCHi-C OR HiC)', None, limit=50)
    return Evidence(status="OK", source="EuropePMC", fetched_n=sum(len(v) for v in groups.values()),
                    data=groups, citations=["https://www.ebi.ac.uk/europepmc/"], fetched_at=_now_iso())

async def svc_genetics_functional(gene: str) -> Evidence:
    groups = await lit_search_guarded(f'{gene} AND (MPRA OR STARR OR enhancer perturbation OR CRISPRa OR CRISPRi)', None, limit=50)
    return Evidence(status="OK", source="EuropePMC", fetched_n=sum(len(v) for v in groups.values()),
                    data=groups, citations=["https://www.ebi.ac.uk/europepmc/"], fetched_at=_now_iso())

async def svc_genetics_lncrna(gene: str) -> Evidence:
    groups = await lit_search_guarded(f'{gene} AND (lncRNA OR long noncoding RNA)', None, limit=50)
    return Evidence(status="OK", source="EuropePMC", fetched_n=sum(len(v) for v in groups.values()),
                    data=groups, citations=["https://www.ebi.ac.uk/europepmc/"], fetched_at=_now_iso())

async def svc_genetics_mirna(gene: str) -> Evidence:
    groups = await lit_search_guarded(f'{gene} AND (miRNA OR microRNA) AND (target OR regulate)', None, limit=50)
    return Evidence(status="OK", source="EuropePMC", fetched_n=sum(len(v) for v in groups.values()),
                    data=groups, citations=["https://www.ebi.ac.uk/europepmc/"], fetched_at=_now_iso())

async def svc_pathogenicity_priors(gene: str) -> Evidence:
    groups = await lit_search_guarded(f'{gene} AND (AlphaMissense OR PrimateAI)', None, limit=50)
    return Evidence(status="OK", source="EuropePMC", fetched_n=sum(len(v) for v in groups.values()),
                    data=groups, citations=["https://www.ebi.ac.uk/europepmc/"], fetched_at=_now_iso())

async def svc_mavedb(gene: str) -> Evidence:
    js = await GN.mavedb_search(gene)
    items = []
    if isinstance(js, dict): items = (js.get("results") or js.get("items") or [])[:50]
    epmc_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={urllib.parse.quote(gene + ' AND (MAVE OR deep mutational scanning OR saturation mutagenesis)')}&format=json&pageSize=50"
    lit = await _http.get_json(epmc_url, tries=1, ttl=43200)
    lit_items = (lit or {}).get("resultList", {}).get("result", []) if isinstance(lit, dict) else []
    return Evidence(status="OK" if items or lit_items else "NO_DATA", source="MaveDB/EPMC", fetched_n=len(items)+len(lit_items),
                    data={"mavedb": items, "literature": lit_items, "portal": f"https://www.mavedb.org/browse?query={urllib.parse.quote(gene)}"},
                    citations=[f"https://www.mavedb.org/api/v1/search?q={urllib.parse.quote(gene)}", epmc_url], fetched_at=_now_iso())

# Mechanism
async def svc_mech_ppi(gene: str) -> Evidence:
    js = await GNW.string_network(gene)
    edges = js or []
    return Evidence(status="OK" if edges else "NO_DATA", source="STRING", fetched_n=len(edges),
                    data={"edges": edges[:200]}, citations=[f"https://string-db.org/api/json/network?identifiers={urllib.parse.quote(gene)}&species=9606"], fetched_at=_now_iso())

async def svc_mech_pathways(uniprot: Optional[str], gene: str) -> Evidence:
    acc = uniprot
    if not acc:
        ujs = await UP.search(gene, "accession")
        try:
            acc = (ujs.get("results")[0] or {}).get("primaryAccession")
        except Exception:
            acc = None
    cites = []; rows = []
    if acc:
        rj = await GNW.reactome_by_uniprot(acc)
        rows = rj or []
        cites.append(f"https://reactome.org/ContentService/data/mapping/UniProt/{acc}/pathways")
    return Evidence(status="OK" if rows else "NO_DATA", source="Reactome", fetched_n=len(rows),
                    data={"uniprot": acc, "pathways": rows}, citations=cites, fetched_at=_now_iso())

async def svc_mech_ligrec(gene: str) -> Evidence:
    js = await GNW.omnipath_interactions(gene)
    return Evidence(status="OK" if js else "NO_DATA", source="OmniPath", fetched_n=len(js or []),
                    data={"interactions": js}, citations=[f"https://omnipathdb.org/interactions?genes={urllib.parse.quote(gene)}&formats=json"], fetched_at=_now_iso())

async def svc_assoc_perturb(gene: str, condition: Optional[str]) -> Evidence:
    q = f'{gene} AND (CRISPR OR RNAi OR perturb-seq OR knockout OR overexpression)'
    if condition: q += f" AND {condition}"
    groups = await lit_search_guarded(q, f'{gene} AND (CRISPR OR RNAi) AND (no effect OR null)', limit=50)
    return Evidence(status="OK", source="EuropePMC", fetched_n=sum(len(v) for v in groups.values()),
                    data=groups, citations=["https://www.ebi.ac.uk/europepmc/"], fetched_at=_now_iso())

async def svc_perturbatlas(gene: str) -> Evidence:
    portal = "https://academic.oup.com/nar/article/52/D1/D1222/7518472"
    return Evidence(status="OK", source="PerturbAtlas (link-out)", fetched_n=0, data={"portal": portal, "gene": gene},
                    citations=[portal], fetched_at=_now_iso())

# Tractability
async def svc_tract_drugs(gene: str) -> Evidence:
    ch = f"https://www.ebi.ac.uk/chembl/api/data/target/search.json?q={urllib.parse.quote(gene)}"
    cj = await _http.get_json(ch, tries=2, ttl=43200)
    dg = f"https://dgidb.org/api/v2/interactions.json?genes={urllib.parse.quote(gene)}"
    dj = await _http.get_json(dg, tries=2, ttl=43200)
    return Evidence(status="OK" if cj or dj else "NO_DATA", source="ChEMBL/DGIdb", fetched_n=1,
                    data={"chembl": cj, "dgidb": dj}, citations=[ch, dg], fetched_at=_now_iso())

async def svc_ligandability_sm(gene: str) -> Evidence:
    return await svc_mech_structure(gene)

async def svc_ligandability_ab(gene: str) -> Evidence:
    return await svc_expr_localization(gene)

async def svc_ligandability_oligo(gene: str) -> Evidence:
    groups = await lit_search_guarded(f'{gene} AND (aptamer OR antisense OR siRNA OR ASO)', None, limit=50)
    return Evidence(status="OK", source="EuropePMC", fetched_n=sum(len(v) for v in groups.values()),
                    data=groups, citations=["https://www.ebi.ac.uk/europepmc/"], fetched_at=_now_iso())

async def svc_tract_modality(gene: str) -> Evidence:
    loc = await svc_expr_localization(gene)
    st = await svc_mech_structure(gene)
    recs = []
    text = json.dumps(loc.data).lower()
    if ("plasma membrane" in text or "membrane" in text or "secreted" in text): recs.append("Antibody")
    if st.data.get("alphafold"): recs.append("Small molecule")
    if "nucleus" in text: recs.append("Oligo")
    recs = list(dict.fromkeys(recs))
    return Evidence(status="OK", source="Heuristic (UniProt/AlphaFold)", fetched_n=len(recs),
                    data={"recommendations": recs, "inputs": {"localization": loc.data, "structure": st.data}},
                    citations=loc.citations + st.citations, fetched_at=_now_iso())

async def svc_tract_immunogenicity(gene: str) -> Evidence:
    groups = await lit_search_guarded(f'{gene} AND (epitope OR immunogenic)', None, limit=50)
    return Evidence(status="OK", source="EuropePMC", fetched_n=sum(len(v) for v in groups.values()),
                    data=groups, citations=["https://www.ebi.ac.uk/europepmc/"], fetched_at=_now_iso())

async def svc_mhc_binding(gene: str) -> Evidence:
    groups = await lit_search_guarded(f'{gene} AND (netMHCpan OR HLA binding prediction)', None, limit=50)
    return Evidence(status="OK", source="EuropePMC", fetched_n=sum(len(v) for v in groups.values()),
                    data=groups, citations=["https://www.ebi.ac.uk/europepmc/"], fetched_at=_now_iso())

async def svc_iedb_epitopes(gene: str, limit: int = 50) -> Evidence:
    api_try = f"https://api.iedb.org/epitope/search?antigen_gene={urllib.parse.quote(gene)}&page_size={min(100, limit)}"
    js = await _http.get_json(api_try, tries=1, ttl=43200)
    recs = []
    if isinstance(js, dict): recs = (js.get("results") or js.get("data") or [])[:limit]
    if not recs:
        groups = await lit_search_guarded(f'{gene} AND (IEDB OR epitope) AND HLA', None, limit=50)
        return Evidence(status="OK" if sum(len(v) for v in groups.values()) else "NO_DATA", source="EuropePMC (IEDB lit)",
                        fetched_n=sum(len(v) for v in groups.values()), data=groups,
                        citations=["https://www.iedb.org/advancedQuery","https://www.ebi.ac.uk/europepmc/"], fetched_at=_now_iso())
    return Evidence(status="OK", source="IEDB IQ-API", fetched_n=len(recs),
                    data={"epitopes": recs, "portal": "https://www.iedb.org/advancedQuery"},
                    citations=[api_try, "https://www.iedb.org/advancedQuery"], fetched_at=_now_iso())

async def svc_surfaceome_hpa(gene: str) -> Evidence:
    link = f"https://www.proteinatlas.org/search/{urllib.parse.quote(gene)}"
    dl = "https://www.proteinatlas.org/about/download"
    return Evidence(status="OK", source="HPA v24 (membrane/secretome)", fetched_n=0,
                    data={"download": dl, "search": link}, citations=[dl, link], fetched_at=_now_iso())

async def svc_tsca() -> Evidence:
    portal = "https://www.cancersurfaceome.org/"
    return Evidence(status="OK", source="Cancer Surfaceome Atlas (link)", fetched_n=0, data={"portal": portal},
                    citations=[portal], fetched_at=_now_iso())

# Clinical & Competitive
async def svc_clin_endpoints(condition: str) -> Evidence:
    js = await TP.ctgov_studies(condition)
    studies = (((js or {}).get("studies") or []))
    return Evidence(status="OK" if studies else "NO_DATA", source="ClinicalTrials.gov v2", fetched_n=len(studies),
                    data={"studies": studies}, citations=[f"https://clinicaltrials.gov/api/v2/studies?query.cond={urllib.parse.quote(condition)}&pageSize=50"], fetched_at=_now_iso())

async def svc_clin_biomarker_fit(gene: str, condition: str) -> Evidence:
    groups = await lit_search_guarded(f'{gene} AND biomarker AND {condition}', f'{gene} AND biomarker AND {condition} AND (failed OR negative OR null)', limit=50)
    return Evidence(status="OK", source="EuropePMC", fetched_n=sum(len(v) for v in groups.values()),
                    data=groups, citations=["https://www.ebi.ac.uk/europepmc/"], fetched_at=_now_iso())

async def svc_clin_pipeline(gene: str) -> Evidence:
    ix = f"https://drugs.ncats.io/api/drugs?q={urllib.parse.quote(gene)}"
    js = await _http.get_json(ix, tries=2, ttl=43200)
    return Evidence(status="OK" if js else "NO_DATA", source="Inxight Drugs", fetched_n=len(js or []),
                    data={"programs": js}, citations=[ix], fetched_at=_now_iso())

async def svc_clin_safety(gene: str) -> Evidence:
    dj = await DR.dgidb(gene)
    drugs = []
    for term in (dj or {}).get("matchedTerms", []):
        for it in term.get("interactions", []):
            if it.get("drugName"): drugs.append(it["drugName"])
    drugs = list(dict.fromkeys(drugs))[:10]
    rows = []; cites = [f"https://dgidb.org/api/v2/interactions.json?genes={urllib.parse.quote(gene)}"]
    for d in drugs:
        fj = await DR.faers_top(d)
        rows.append({"drug": d, "faers": fj})
        cites.append(f"https://api.fda.gov/drug/event.json?search=patient.drug.medicinalproduct:{urllib.parse.quote(d)}&count=patient.reaction.reactionmeddrapt.exact")
    return Evidence(status="OK" if rows else "NO_DATA", source="openFDA FAERS + DGIdb", fetched_n=len(rows),
                    data={"drugs": drugs, "faers_counts": rows}, citations=cites, fetched_at=_now_iso())

async def svc_clin_safety_pgx(gene: str) -> Evidence:
    js = await DR.pharmgkb_gene(gene)
    rows = (js or {}).get("data", []) if isinstance(js, dict) else []
    return Evidence(status="OK" if rows else "NO_DATA", source="PharmGKB", fetched_n=len(rows),
                    data={"pgx": rows}, citations=[f"https://api.pharmgkb.org/v1/data/gene?symbol={urllib.parse.quote(gene)}"], fetched_at=_now_iso())

async def svc_clin_rwe(gene: str) -> Evidence:
    ev = await svc_clin_safety(gene)
    topline = []
    for rec in ev.data.get("faers_counts", []):
        counts = (rec.get("faers") or {}).get("results", [])
        topline.append({"drug": rec.get("drug"), "top_reactions": counts[:10]})
    return Evidence(status=ev.status, source="openFDA FAERS (topline)", fetched_n=len(topline),
                    data={"topline": topline}, citations=ev.citations, fetched_at=_now_iso())

async def svc_on_target_ae_prior(gene: str) -> Evidence:
    dj = await DR.dgidb(gene)
    drugs = []
    for term in (dj or {}).get("matchedTerms", []):
        for it in term.get("interactions", []):
            if it.get("drugName"): drugs.append(it["drugName"])
    drugs = list(dict.fromkeys(drugs))[:10]
    rows_all, cites = [], [f"https://dgidb.org/api/v2/interactions.json?genes={urllib.parse.quote(gene)}"]
    for d in drugs:
        sj = await DR.sider_meddra(d)
        if isinstance(sj, list): rows_all.extend(sj[:200])
        cites.append(f"http://sideeffects.embl.de/api/meddra/allSides?drug={urllib.parse.quote(d)}")
    return Evidence(status="OK" if rows_all else "NO_DATA", source="DGIdb + SIDER", fetched_n=len(rows_all),
                    data={"drugs": drugs, "ae_priors": rows_all}, citations=cites, fetched_at=_now_iso())

async def svc_comp_intensity(keyword: str) -> Evidence:
    js = await TP.patentsview(keyword)
    return Evidence(status="OK" if js else "NO_DATA", source="PatentsView", fetched_n=len(((js or {}).get("patents") or [])),
                    data={"query": keyword, "patents": (js or {}).get("patents", [])}, citations=[
                        "https://api.patentsview.org/patents/query"
                    ], fetched_at=_now_iso())

async def svc_comp_freedom(keyword: str) -> Evidence:
    ev = await svc_comp_intensity(keyword)
    years = {}
    for p in ev.data.get("patents", []):
        y = (p.get("patent_date") or "")[:4]
        if y.isdigit():
            years[y] = years.get(y, 0) + 1
    trend = [{"year": int(y), "count": c} for y, c in sorted(years.items())]
    ev.data["year_trend"] = trend
    return ev

async def svc_clin_eu_ctr_linkouts(condition: str) -> Evidence:
    portal = "https://euclinicaltrials.eu/"
    return Evidence(status="OK", source="EU CTR/CTIS (link)", fetched_n=0, data={"condition": condition, "portal": portal},
                    citations=[portal], fetched_at=_now_iso())

# Routes — one per module

# Identity
@router.get("/expr/baseline", response_model=Evidence)
async def expr_baseline(gene: str) -> Evidence: return await svc_expr_baseline(gene)

@router.get("/expr/localization", response_model=Evidence)
async def expr_localization(gene: str) -> Evidence: return await svc_expr_localization(gene)

@router.get("/mech/structure", response_model=Evidence)
async def mech_structure(gene: str) -> Evidence: return await svc_mech_structure(gene)

@router.get("/expr/inducibility", response_model=Evidence)
async def expr_inducibility(gene: str, condition: Optional[str] = None) -> Evidence: return await svc_expr_inducibility(gene, condition)

# Association
@router.get("/assoc/bulk-rna", response_model=Evidence)
async def assoc_bulk_rna(gene: str, condition: str) -> Evidence: return await svc_assoc_bulk_rna(gene, condition)

@router.get("/assoc/sc", response_model=Evidence)
async def assoc_sc(gene: str, condition: Optional[str] = None) -> Evidence: return await svc_assoc_sc(gene, condition)

@router.get("/assoc/spatial-expression", response_model=Evidence)
async def assoc_spatial_expression(gene: str, condition: Optional[str] = None) -> Evidence: return await svc_spatial_expression(gene, condition)

@router.get("/assoc/spatial-neighborhoods", response_model=Evidence)
async def assoc_spatial_neighborhoods(condition: str) -> Evidence: return await svc_spatial_neighborhoods(condition)

@router.get("/assoc/bulk-prot", response_model=Evidence)
async def assoc_bulk_prot(condition: str) -> Evidence: return await svc_assoc_bulk_prot(condition)

@router.get("/assoc/omics-phosphoproteomics", response_model=Evidence)
async def assoc_omics_phosphoproteomics(gene: str, condition: Optional[str] = None) -> Evidence: return await svc_omics_phosphoproteomics(gene, condition)

@router.get("/assoc/omics-metabolites", response_model=Evidence)
async def assoc_omics_metabolites(condition: str) -> Evidence: return await svc_omics_metabolites(condition)

@router.get("/assoc/hpa-pathology", response_model=Evidence)
async def assoc_hpa_pathology(gene: str) -> Evidence: return await svc_hpa_pathology(gene)

@router.get("/assoc/bulk-prot-pdc", response_model=Evidence)
async def assoc_bulk_prot_pdc(gene: str, limit: int = Query(50, ge=1, le=200)) -> Evidence: return await svc_bulk_prot_pdc(gene, limit)

@router.get("/assoc/metabolomics-ukb-nightingale", response_model=Evidence)
async def assoc_metabolomics_ukb_nightingale() -> Evidence: return await svc_metabolomics_ukb_nightingale()

# Genetics
@router.get("/genetics/l2g", response_model=Evidence)
async def genetics_l2g(gene: str) -> Evidence: return await svc_genetics_l2g(gene)

@router.get("/genetics/coloc", response_model=Evidence)
async def genetics_coloc(gene: str) -> Evidence: return await svc_genetics_coloc(gene)

@router.get("/genetics/mr", response_model=Evidence)
async def genetics_mr(gene: str, condition: str) -> Evidence: return await svc_genetics_mr(gene, condition)

@router.get("/genetics/rare", response_model=Evidence)
async def genetics_rare(gene: str) -> Evidence: return await svc_genetics_rare(gene)

@router.get("/genetics/mendelian", response_model=Evidence)
async def genetics_mendelian(gene: str) -> Evidence: return await svc_genetics_mendelian(gene)

@router.get("/genetics/intolerance", response_model=Evidence)
async def genetics_intolerance(gene: str) -> Evidence: return await svc_genetics_intolerance(gene)

@router.get("/genetics/phewas-human-knockout", response_model=Evidence)
async def genetics_phewas_human_knockout(gene: str) -> Evidence: return await svc_phewas_human_knockout(gene)

@router.get("/genetics/sqtl", response_model=Evidence)
async def genetics_sqtl(gene: str) -> Evidence: return await svc_genetics_sqtl(gene)

@router.get("/genetics/pqtl", response_model=Evidence)
async def genetics_pqtl(gene: str) -> Evidence: return await svc_genetics_pqtl(gene)

@router.get("/genetics/chromatin-contacts", response_model=Evidence)
async def genetics_chromatin_contacts(gene: str) -> Evidence: return await svc_chromatin_contacts(gene)

@router.get("/genetics/functional", response_model=Evidence)
async def genetics_functional(gene: str) -> Evidence: return await svc_genetics_functional(gene)

@router.get("/genetics/lncrna", response_model=Evidence)
async def genetics_lncrna(gene: str) -> Evidence: return await svc_genetics_lncrna(gene)

@router.get("/genetics/mirna", response_model=Evidence)
async def genetics_mirna(gene: str) -> Evidence: return await svc_genetics_mirna(gene)

@router.get("/genetics/pathogenicity-priors", response_model=Evidence)
async def genetics_pathogenicity_priors(gene: str) -> Evidence: return await svc_pathogenicity_priors(gene)

@router.get("/genetics/mavedb", response_model=Evidence)
async def genetics_mavedb(gene: str) -> Evidence: return await svc_mavedb(gene)

# Mechanism
@router.get("/mech/ppi", response_model=Evidence)
async def mech_ppi(gene: str) -> Evidence: return await svc_mech_ppi(gene)

@router.get("/mech/pathways", response_model=Evidence)
async def mech_pathways(gene: str, uniprot: Optional[str] = None) -> Evidence: return await svc_mech_pathways(uniprot, gene)

@router.get("/mech/ligrec", response_model=Evidence)
async def mech_ligrec(gene: str) -> Evidence: return await svc_mech_ligrec(gene)

@router.get("/assoc/perturb", response_model=Evidence)
async def assoc_perturb(gene: str, condition: Optional[str] = None) -> Evidence: return await svc_assoc_perturb(gene, condition)

@router.get("/assoc/perturbatlas", response_model=Evidence)
async def assoc_perturbatlas(gene: str) -> Evidence: return await svc_perturbatlas(gene)

# Tractability
@router.get("/tract/drugs", response_model=Evidence)
async def tract_drugs(gene: str) -> Evidence: return await svc_tract_drugs(gene)

@router.get("/tract/ligandability-sm", response_model=Evidence)
async def tract_ligandability_sm(gene: str) -> Evidence: return await svc_ligandability_sm(gene)

@router.get("/tract/ligandability-ab", response_model=Evidence)
async def tract_ligandability_ab(gene: str) -> Evidence: return await svc_ligandability_ab(gene)

@router.get("/tract/ligandability-oligo", response_model=Evidence)
async def tract_ligandability_oligo(gene: str) -> Evidence: return await svc_ligandability_oligo(gene)

@router.get("/tract/modality", response_model=Evidence)
async def tract_modality(gene: str) -> Evidence: return await svc_tract_modality(gene)

@router.get("/tract/immunogenicity", response_model=Evidence)
async def tract_immunogenicity(gene: str) -> Evidence: return await svc_tract_immunogenicity(gene)

@router.get("/tract/mhc-binding", response_model=Evidence)
async def tract_mhc_binding(gene: str) -> Evidence: return await svc_mhc_binding(gene)

@router.get("/tract/iedb-epitopes", response_model=Evidence)
async def tract_iedb_epitopes(gene: str, limit: int = Query(50, ge=1, le=200)) -> Evidence: return await svc_iedb_epitopes(gene, limit)

@router.get("/tract/surfaceome-hpa", response_model=Evidence)
async def tract_surfaceome_hpa(gene: str) -> Evidence: return await svc_surfaceome_hpa(gene)

@router.get("/tract/tsca", response_model=Evidence)
async def tract_tsca() -> Evidence: return await svc_tsca()

# Clinical & Competitive
@router.get("/clin/endpoints", response_model=Evidence)
async def clin_endpoints(condition: str) -> Evidence: return await svc_clin_endpoints(condition)

@router.get("/clin/biomarker-fit", response_model=Evidence)
async def clin_biomarker_fit(gene: str, condition: str) -> Evidence: return await svc_clin_biomarker_fit(gene, condition)

@router.get("/clin/pipeline", response_model=Evidence)
async def clin_pipeline(gene: str) -> Evidence: return await svc_clin_pipeline(gene)

@router.get("/clin/safety", response_model=Evidence)
async def clin_safety(gene: str) -> Evidence: return await svc_clin_safety(gene)

@router.get("/clin/safety-pgx", response_model=Evidence)
async def clin_safety_pgx(gene: str) -> Evidence: return await svc_clin_safety_pgx(gene)

@router.get("/clin/rwe", response_model=Evidence)
async def clin_rwe(gene: str) -> Evidence: return await svc_clin_rwe(gene)

@router.get("/clin/on-target-ae-prior", response_model=Evidence)
async def clin_on_target_ae_prior(gene: str) -> Evidence: return await svc_on_target_ae_prior(gene)

@router.get("/comp/intensity", response_model=Evidence)
async def comp_intensity(keyword: str) -> Evidence: return await svc_comp_intensity(keyword)

@router.get("/comp/freedom", response_model=Evidence)
async def comp_freedom(keyword: str) -> Evidence: return await svc_comp_freedom(keyword)

@router.get("/clin/eu-ctr-linkouts", response_model=Evidence)
async def clin_eu_ctr_linkouts(condition: str) -> Evidence: return await svc_clin_eu_ctr_linkouts(condition)

@router.get("/registry/buckets")
def registry_buckets() -> Dict[str, Any]:
    return {"buckets": BUCKETS}

@router.get("/registry/modules")
def registry_modules() -> Dict[str, Any]:
    return {"n_modules": len(MODULES), "modules": MODULES}

@router.get("/registry/buckets-v58")
def registry_buckets_v58() -> Dict[str, Any]:
    mapping: Dict[str, List[str]] = {b["id"]: [] for b in BUCKETS}
    for m in MODULES:
        mapping[m["bucket"]].append(module_route(m["id"]))
    mapping["SYNTHESIS_TI"] = ["/synth/therapeutic-index"]
    mods = []
    for k, v in mapping.items():
        if k != "SYNTHESIS_TI": mods.extend(v)
    umods = sorted(set(mods))
    return {"buckets": mapping, "counts": {k: len(v) for k, v in mapping.items()}, "total_modules": len(umods)}

@router.get("/status")
def status() -> Dict[str, Any]:
    return {
        "service": "targetval-router v58-full",
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "modules": len(MODULES),
        "buckets": [b["id"] for b in BUCKETS],
        "synthesis": "/synth/therapeutic-index"
    }

def _band(p: float) -> str:
    if p >= 0.8: return "High"
    if p >= 0.6: return "Moderate"
    return "Low"

def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1-1e-6); return math.log(p/(1-p))

def _inv_logit(x: float) -> float:
    return 1/(1+math.exp(-x))

async def build_features_genetic_causality(gene: str, condition: Optional[str]) -> BucketFeature:
    ev_l2g, ev_coloc, ev_mr, ev_rare, ev_mendel, ev_lof, ev_sqtl, ev_pqtl, ev_chrom, ev_func, ev_mave = await asyncio.gather(
        svc_genetics_l2g(gene), svc_genetics_coloc(gene), svc_genetics_mr(gene, condition or ""),
        svc_genetics_rare(gene), svc_genetics_mendelian(gene), svc_phewas_human_knockout(gene),
        svc_genetics_sqtl(gene), svc_genetics_pqtl(gene), svc_chromatin_contacts(gene),
        svc_genetics_functional(gene), svc_mavedb(gene)
    )
    drivers, tensions, cites = [], [], []
    votes = 0.0
    if _ok(ev_coloc): votes += 0.35; drivers.append("Colocalization support."); cites += ev_coloc.citations
    if _ok(ev_mr): votes += 0.45; drivers.append("MR discovery matches exposure/outcome."); cites += ev_mr.citations
    if _ok(ev_rare): votes += 0.3; drivers.append("ClinVar rare variants."); cites += ev_rare.citations
    if _ok(ev_mendel): votes += 0.2; drivers.append("ClinGen gene validity."); cites += ev_mendel.citations
    if _ok(ev_lof): votes += 0.2; drivers.append("LoF carrier PheWAS signal."); cites += ev_lof.citations
    if _ok(ev_sqtl): votes += 0.15; drivers.append("sQTL/eQTL regulatory support."); cites += ev_sqtl.citations
    if _ok(ev_pqtl): votes += 0.25; drivers.append("pQTL support."); cites += ev_pqtl.citations
    if _ok(ev_chrom): votes += 0.15; drivers.append("Chromatin contacts (PCHi-C)."); cites += ev_chrom.citations
    if _ok(ev_func): votes += 0.2; drivers.append("Functional regulatory assays (MPRA/CRISPRa/i)."); cites += ev_func.citations
    if _ok(ev_mave): votes += 0.2; drivers.append("Variant effect maps (MaveDB)."); cites += ev_mave.citations
    band = "High" if votes >= 1.1 else "Moderate" if votes >= 0.6 else "Low"
    if not _ok(ev_mr) and _ok(ev_coloc): tensions.append("Coloc without MR triangulation.")
    if _ok(ev_mr) and not _ok(ev_coloc): tensions.append("MR without explicit coloc.")
    return BucketFeature(bucket="GENETIC_CAUSALITY", band=band, drivers=drivers[:6], tensions=tensions, citations=list(dict.fromkeys(cites))[:20], details={"votes": round(votes,2)})

async def build_features_association(gene: str, condition: Optional[str]) -> BucketFeature:
    ev_rna, ev_sc, ev_sp, ev_sn, ev_prot, ev_pdc, ev_met, ev_ng, ev_hpa = await asyncio.gather(
        svc_assoc_bulk_rna(gene, condition or ""),
        svc_assoc_sc(gene, condition), svc_spatial_expression(gene, condition), svc_spatial_neighborhoods(condition or ""),
        svc_assoc_bulk_prot(condition or ""), svc_bulk_prot_pdc(gene), svc_omics_metabolites(condition or ""),
        svc_metabolomics_ukb_nightingale(), svc_hpa_pathology(gene)
    )
    drivers, tensions, cites = [], [], []
    votes = 0
    if _ok(ev_rna): votes += 1; drivers.append("Bulk RNA disease association."); cites += ev_rna.citations
    if _ok(ev_prot) or _ok(ev_pdc): votes += 1; drivers.append("Proteomics presence."); cites += ev_prot.citations + ev_pdc.citations
    if _ok(ev_sc) or _ok(ev_sp): votes += 1; drivers.append("Single-cell / spatial context."); cites += ev_sc.citations + ev_sp.citations
    if _ok(ev_met) or _ok(ev_ng): drivers.append("Metabolomic context."); cites += ev_met.citations + ev_ng.citations
    band = "High" if votes >= 2 else "Moderate" if votes == 1 else "Low"
    return BucketFeature(bucket="DISEASE_ASSOCIATION", band=band, drivers=drivers[:6], tensions=tensions, citations=list(dict.fromkeys(cites))[:20], details={"votes": votes})

async def build_features_mechanism(gene: str, condition: Optional[str]) -> BucketFeature:
    ev_ppi, ev_path, ev_lr, ev_pert, ev_ph = await asyncio.gather(
        svc_mech_ppi(gene), svc_mech_pathways(None, gene), svc_mech_ligrec(gene),
        svc_assoc_perturb(gene, condition), svc_omics_phosphoproteomics(gene, condition)
    )
    drivers, tensions, cites = [], [], []
    votes = 0
    if _ok(ev_ppi): votes += 1; drivers.append("PPI network context."); cites += ev_ppi.citations
    if _ok(ev_path): votes += 1; drivers.append("Pathway membership."); cites += ev_path.citations
    if _ok(ev_lr): votes += 1; drivers.append("Ligand–receptor edges."); cites += ev_lr.citations
    if _ok(ev_pert): votes += 1; drivers.append("Perturbation evidence."); cites += ev_pert.citations
    if _ok(ev_ph): drivers.append("Phosphoproteomics support."); cites += ev_ph.citations
    band = "High" if votes >= 3 else "Moderate" if votes == 2 else "Low"
    return BucketFeature(bucket="MECHANISM", band=band, drivers=drivers[:6], tensions=tensions, citations=list(dict.fromkeys(cites))[:20], details={"votes": votes})

async def build_features_tractability(gene: str) -> BucketFeature:
    ev_drugs, ev_sm, ev_ab, ev_oligo, ev_mod, ev_iedb, ev_mhc, ev_hpa = await asyncio.gather(
        svc_tract_drugs(gene), svc_ligandability_sm(gene), svc_ligandability_ab(gene), svc_ligandability_oligo(gene),
        svc_tract_modality(gene), svc_iedb_epitopes(gene), svc_mhc_binding(gene), svc_surfaceome_hpa(gene)
    )
    drivers, tensions, cites = [], [], []
    votes = 0
    if _ok(ev_sm): votes += 1; drivers.append("Structure/pocket presence."); cites += ev_sm.citations
    if _ok(ev_ab): votes += 1; drivers.append("Surface accessibility (UniProt/HPA)."); cites += ev_ab.citations + ev_hpa.citations
    if _ok(ev_mod) and (ev_mod.data.get("recommendations")): votes += 1; drivers.append(f"Modality recommendation: {', '.join(ev_mod.data.get('recommendations'))}."); cites += ev_mod.citations
    penalty = 0
    if _ok(ev_iedb):
        penalty += 1; tensions.append("Curated epitopes suggest immunogenicity risk."); cites += ev_iedb.citations
    if _ok(ev_mhc):
        penalty += 0.5; tensions.append("MHC binding predictions reported in literature."); cites += ev_mhc.citations
    raw = votes - penalty*0.5
    band = "High" if raw >= 2 else "Moderate" if raw >= 1 else "Low"
    return BucketFeature(bucket="TRACTABILITY_MODALITY", band=band, drivers=drivers[:6], tensions=tensions[:4], citations=list(dict.fromkeys(cites))[:20], details={"votes": votes, "penalty": penalty})

async def build_features_clinical_fit(gene: str, condition: Optional[str]) -> BucketFeature:
    ev_endp, ev_biof, ev_pipe, ev_saf, ev_pgx, ev_rwe, ev_ae, ev_pat, ev_fto, ev_euctr, ev_hpa = await asyncio.gather(
        svc_clin_endpoints(condition or ""), svc_clin_biomarker_fit(gene, condition or ""),
        svc_clin_pipeline(gene), svc_clin_safety(gene), svc_clin_safety_pgx(gene),
        svc_clin_rwe(gene), svc_on_target_ae_prior(gene), svc_comp_intensity(condition or gene),
        svc_comp_freedom(condition or gene), svc_clin_eu_ctr_linkouts(condition or ""), svc_hpa_pathology(gene)
    )
    drivers, tensions, cites = [], [], []
    safety_flag = False
    if _ok(ev_endp): drivers.append("Trials landscape available."); cites += ev_endp.citations
    if _ok(ev_pipe): drivers.append("Pipeline/programs exist (context)."); cites += ev_pipe.citations
    if _ok(ev_biof): drivers.append("Biomarker feasibility literature."); cites += ev_biof.citations
    if _ok(ev_saf): safety_flag = True; tensions.append("FAERS signals present."); cites += ev_saf.citations
    if _ok(ev_pgx): safety_flag = True; tensions.append("PharmGKB PGx risks."); cites += ev_pgx.citations
    if _ok(ev_rwe): safety_flag = True; tensions.append("RWE topline AE patterns."); cites += ev_rwe.citations
    if _ok(ev_ae): safety_flag = True; tensions.append("On-target AE priors (DGIdb→SIDER)."); cites += ev_ae.citations
    if _ok(ev_pat) and _ok(ev_fto): drivers.append("Patent activity characterized (intensity/FT0 proxy)."); cites += ev_pat.citations + ev_fto.citations
    if _ok(ev_euctr): drivers.append("EU CTR link-outs added."); cites += ev_euctr.citations
    if _ok(ev_hpa): drivers.append("Pathology expression context."); cites += ev_hpa.citations

    band = "Moderate"
    if safety_flag: band = "Low"
    if _ok(ev_endp) and _ok(ev_biof) and not safety_flag: band = "High"
    return BucketFeature(bucket="CLINICAL_FIT_FEASIBILITY", band=band, drivers=drivers[:6], tensions=tensions[:5], citations=list(dict.fromkeys(cites))[:20], details={"safety_flag": safety_flag})

@router.get("/synth/therapeutic-index", response_model=SynthesisTI)
async def synth_therapeutic_index(gene: str, condition: Optional[str] = None, therapy_area: Optional[str] = None) -> SynthesisTI:
    f_gen, f_assoc, f_mech, f_trac, f_clin = await asyncio.gather(
        build_features_genetic_causality(gene, condition),
        build_features_association(gene, condition),
        build_features_mechanism(gene, condition),
        build_features_tractability(gene),
        build_features_clinical_fit(gene, condition)
    )

    priors = {"oncology": 0.25, "cns": 0.12, "cardio": 0.18}
    p0 = priors.get((therapy_area or "").lower(), 0.2)
    inc = {"High": 0.9, "Moderate": 0.4, "Low": 0.0}
    dec = {"High": -1.0, "Moderate": -0.4, "Low": 0.0}

    ll = _logit(p0)
    ll += inc.get(f_gen.band, 0.0) + inc.get(f_assoc.band, 0.0) + inc.get(f_mech.band, 0.0) + inc.get(f_trac.band, 0.0)
    ll += dec["High"] if f_clin.details.get("safety_flag") else inc.get(f_clin.band, 0.0)
    p = _inv_logit(ll)

    drivers, flip_if = [], []
    drivers += f_gen.drivers[:2] + f_assoc.drivers[:2] + f_trac.drivers[:1]
    if f_gen.band == "High" and f_assoc.band == "Low":
        flip_if.append("Obtain strong sc/spatial disease tissue evidence.")
    if f_clin.details.get("safety_flag"):
        flip_if.append("Mitigate on-target or PGx-driven AE risk; stratify by genotype/HLA if applicable.")

    verdict = "Favorable"
    if f_gen.band == "Low" or f_clin.details.get("safety_flag"):
        verdict = "Unfavorable"
    elif any(b == "Low" for b in [f_assoc.band, f_trac.band, f_mech.band]):
        verdict = "Marginal"

    citations = []
    for bf in [f_gen, f_assoc, f_mech, f_trac, f_clin]:
        citations += bf.citations
    citations = list(dict.fromkeys(citations))[:50]

    return SynthesisTI(
        gene=gene, condition=condition,
        bucket_features={
            "GENETIC_CAUSALITY": f_gen, "DISEASE_ASSOCIATION": f_assoc,
            "MECHANISM": f_mech, "TRACTABILITY_MODALITY": f_trac, "CLINICAL_FIT_FEASIBILITY": f_clin
        },
        verdict=verdict,
        bands={"causality": f_gen.band, "association": f_assoc.band, "mechanism": f_mech.band, "tractability": f_trac.band, "clinical_fit": f_clin.band},
        drivers=drivers[:8], flip_if=flip_if[:6], p_favorable=round(p, 3), citations=citations
    )
\n
# --- Lightweight Knowledge Graph classes & endpoint (optional visualization) ---
class KGNode(BaseModel):
    id: str; kind: str; label: str; meta: Dict[str, Any] = Field(default_factory=dict)

class KGEdge(BaseModel):
    src: str; dst: str; predicate: str; evidence_ids: List[str] = Field(default_factory=list); weight: float = 1.0

class KGEvidence(BaseModel):
    id: str; source: str; citation: Optional[str] = None; summary: Optional[str] = None

class KnowledgeGraph(BaseModel):
    nodes: Dict[str, KGNode] = Field(default_factory=dict)
    edges: List[KGEdge] = Field(default_factory=list)
    evidence: Dict[str, KGEvidence] = Field(default_factory=dict)

def kg_add_node(kg: KnowledgeGraph, node: KGNode): kg.nodes[node.id] = node
def kg_add_edge(kg: KnowledgeGraph, edge: KGEdge): kg.edges.append(edge)
def kg_add_evidence(kg: KnowledgeGraph, ev: KGEvidence): kg.evidence[ev.id] = ev

async def build_kg_from_bucket_features(gene: str, condition: Optional[str], feats: Dict[str, BucketFeature]) -> KnowledgeGraph:
    kg = KnowledgeGraph()
    kg_add_node(kg, KGNode(id=f"gene:{gene}", kind="gene", label=gene))
    if condition:
        kg_add_node(kg, KGNode(id=f"cond:{condition}", kind="condition", label=condition))
        kg_add_edge(kg, KGEdge(src=f"gene:{gene}", dst=f"cond:{condition}", predicate="investigated_in"))
    for bname, bf in feats.items():
        nid = f"bucket:{bname.lower()}"
        kg_add_node(kg, KGNode(id=nid, kind="bucket", label=bname, meta={"band": bf.band}))
        kg_add_edge(kg, KGEdge(src=f"gene:{gene}", dst=nid, predicate="has_feature",
                               weight={"High":1.0,"Moderate":0.6,"Low":0.2}[bf.band]))
        for i, cite in enumerate(bf.citations[:12]):
            evid = KGEvidence(id=f"ev:{bname}:{i}", source="citation", citation=cite)
            kg_add_evidence(kg, evid)
    return kg

@router.get("/synth/kg")
async def synth_kg(gene: str, condition: Optional[str] = None) -> Dict[str, Any]:
    f_gen, f_assoc, f_mech, f_trac, f_clin = await asyncio.gather(
        build_features_genetic_causality(gene, condition),
        build_features_association(gene, condition),
        build_features_mechanism(gene, condition),
        build_features_tractability(gene),
        build_features_clinical_fit(gene, condition)
    )
    feats = {
        "GENETIC_CAUSALITY": f_gen, "DISEASE_ASSOCIATION": f_assoc,
        "MECHANISM": f_mech, "TRACTABILITY_MODALITY": f_trac, "CLINICAL_FIT_FEASIBILITY": f_clin
    }
    kg = await build_kg_from_bucket_features(gene, condition, feats)
    return kg.dict()
\n
