# Engineering on-call handbook

On-call keeps our services running outside office hours. Every engineer from level L2 joins the rotation after their first three months.

## Rotation

The rotation is weekly. Handover happens every Monday at 10:00. Each week has a primary and a secondary on-call engineer. You can swap shifts with a colleague; update the schedule yourself before the swap starts.

## Paging and acknowledgement

Alerts page the primary on-call engineer through the paging app. Acknowledge a page within 5 minutes. If the primary doesn't acknowledge within 15 minutes, the page escalates to the secondary, and after 30 minutes to the engineering manager.

## Severity levels

- SEV1: customers can't use a core product. Start an incident channel immediately and page the incident commander.
- SEV2: a major feature is degraded for many customers. Fix or mitigate within four hours.
- SEV3: minor impact or a workaround exists. Fix during office hours.

## Compensation

Each on-call week is paid with an allowance of 150 EUR. If you are paged outside office hours during a weekend, you get one extra day off, to be taken within the following month. Time spent on incidents at night counts as working time.

## Handover checklist

At handover, the outgoing engineer writes down open incidents, flaky alerts and planned risky changes. The incoming engineer checks that the paging app reaches their phone with a test page.

## After an incident

Every SEV1 and SEV2 incident gets a blameless post-mortem within five working days. The post-mortem lists the timeline, the root cause and follow-up actions with owners.
