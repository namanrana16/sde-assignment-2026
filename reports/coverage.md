# Test coverage of controller/

Command: `python -m pytest -q tests --cov=controller --cov-report=term` (pytest-cov, dev-only; not in requirements.txt). Result: 685 passed.

```
Name                                    Stmts   Miss  Cover
-----------------------------------------------------------
controller\__init__.py                      1      0   100%
controller\__main__.py                    211     36    83%
controller\alerts.py                      216      6    97%
controller\audit.py                       194     11    94%
controller\audit_render.py                174     27    84%
controller\audit_story.py                 108     20    81%
controller\baselines.py                    60      1    98%
controller\client.py                      184      6    97%
controller\cohorts.py                     168      0   100%
controller\configlib.py                   223     19    91%
controller\credits.py                      36      0   100%
controller\cube.py                         54      0   100%
controller\desired.py                      95      3    97%
controller\detectors\__init__.py            0      0   100%
controller\detectors\common.py             26      1    96%
controller\detectors\did.py               289      4    99%
controller\detectors\did_data.py           46      0   100%
controller\detectors\did_eval.py          159      3    98%
controller\detectors\did_owner.py          48      5    90%
controller\detectors\did_plan.py           94      3    97%
controller\detectors\did_solo.py           85      1    99%
controller\detectors\fatigue_curve.py     109      4    96%
controller\detectors\halluc.py            418     19    95%
controller\detectors\halluc_data.py        75      1    99%
controller\detectors\prompt_safety.py     156     38    76%
controller\dryrun.py                      148      2    99%
controller\exp_alloc.py                    36      0   100%
controller\exp_data.py                     97      2    98%
controller\exp_decay.py                   115     11    90%
controller\exp_entry.py                    44      3    93%
controller\exp_interactions.py             38      0   100%
controller\exp_proposals.py                31      3    90%
controller\exp_reconcile.py                22      0   100%
controller\exp_rules.py                   137      4    97%
controller\exp_state.py                    82      3    96%
controller\exp_stats.py                   138      2    99%
controller\experiments.py                 468     40    91%
controller\fatigue.py                     104      2    98%
controller\faults.py                       22      0   100%
controller\governance.py                  359     33    91%
controller\incidents.py                    91     11    88%
controller\ingest.py                      245      4    98%
controller\levers.py                       19      1    95%
controller\loop.py                        223      9    96%
controller\metrics.py                      65      3    95%
controller\person_analysis.py             221      5    98%
controller\planner.py                     128      9    93%
controller\proposal_repo.py               112      0   100%
controller\proposals.py                   105      2    98%
controller\schedule.py                    234      6    97%
controller\schedule_model.py              141      3    98%
controller\settings.py                    179      0   100%
controller\stats.py                        89      2    98%
controller\store.py                       136      4    97%
controller\ui.py                          148     12    92%
controller\view.py                         87     15    83%
controller\view_data.py                   217     27    88%
controller\world_id.py                     81      1    99%
-----------------------------------------------------------
TOTAL                                    7591    427    94%
```
