# WEKA module

A dedicated module to pre-create external WEKA filesystems before providing their IDs as
`[jail | jail_submount].existing.id`.

However it's possible to create the filesystem outside the Soperator installation,
the use of this module allows to store FS configurations as code and in TF state. 
